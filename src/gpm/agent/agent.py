"""Agent：注册 → 心跳/配置同步（拉模式）→ 本地调度探测 → 批量上报（离线缓冲）。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..common.dnsres import DnsCache, DnsError, is_fake_ip, resolve_a, resolve_aaaa
from ..common.util import IS_WINDOWS, is_ip, now, run_cmd
from ..probers.curl import run_curl
from ..probers.dnsmon import run_dnsmon
from ..probers.mtr import run_mtr
from ..probers.ping import run_ping
from ..probers.tcp import run_tcp, split_host_port

# dns 值变更记忆容量：最多记多少个任务的「上次答案集」（LRU 式按插入序淘汰）
DNS_MEMORY_MAX = 500

log = logging.getLogger("gpm.agent")


def _post_json(url: str, payload: dict, timeout: float = 10) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "gpm-agent/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:  # noqa
            pass
        return e.code, {"detail": body}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {"detail": str(e)}


def _system_info() -> dict:
    """节点系统信息：OS 名称/版本/内核/架构 + Python（供「节点详情」展示）。"""
    import platform
    info = {
        "os": platform.system().lower() or ("windows" if IS_WINDOWS else "linux"),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "pretty": platform.platform(),
    }
    try:
        if IS_WINDOWS:
            import winreg  # noqa: PLC0415 - 仅 Windows 存在
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
            vals = {}
            for name in ("ProductName", "DisplayVersion", "CurrentBuild", "UBR"):
                try:
                    vals[name] = str(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
            winreg.CloseKey(key)
            if vals.get("ProductName"):
                build = vals.get("CurrentBuild", "")
                ubr = vals.get("UBR", "")
                info["pretty"] = " ".join(x for x in (
                    vals["ProductName"], vals.get("DisplayVersion", ""),
                    f"build {build}.{ubr}" if build else "") if x)
        else:
            with open("/etc/os-release", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        info["pretty"] = line.split("=", 1)[1].strip().strip('"')
                        break
    except Exception:  # noqa: BLE001 - 系统信息拿不到不影响探测
        pass
    return info


def _local_ip(server_url: str) -> str:
    """本机 IP：向服务端地址做一次 UDP connect（不发包），取所用网卡的源地址。"""
    import socket
    import urllib.parse
    try:
        u = urllib.parse.urlparse(server_url)
        host, port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, port))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:  # noqa: BLE001
        return ""


def _url_hostname(target: str) -> str:
    """从 URL/目标里取出用于 DNS 解析的主机名（剥掉 scheme 后的端口）。

    "http://127.0.0.1:8622/api/health" → "127.0.0.1"；"example.com:8080" → "example.com"；
    "[::1]:8080" → "::1"。裸 IPv6 字面量（多个冒号）原样返回，避免误切。
    """
    host = target.split("/")[2] if "://" in target else target
    if host.startswith("["):                       # [v6]:port
        end = host.find("]")
        host = host[1:end] if end > 0 else host.strip("[]")
    elif host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host = host.rsplit(":", 1)[0]
    return host


def _system_stats(tasks: int = 0) -> dict:
    """节点资源快照。tasks = 当前调度的探测流数量，随心跳上报给服务端。"""
    stats = {"version": "0.1.0", "tasks": tasks}
    try:
        import psutil  # 可选
        vm = psutil.virtual_memory()
        stats["cpu"] = psutil.cpu_percent(interval=None)
        stats["mem"] = vm.percent
    except Exception:  # noqa
        # psutil 缺失（如精简容器）→ 上报 null，服务端存 NULL，UI 显示「—」，
        # 避免把「采集不到」伪装成 0% 占用
        stats["cpu"] = None
        stats["mem"] = None
    return stats


def resolve_for(task: dict, dns_server: str, cache: DnsCache,
                dns_timeout: float, ip_version: str = "auto") -> tuple[str, float | None, str]:
    """curl/mtr/tcp 的先解析后探测。返回 (ip, dns_ms, dns_label)。

    ip_version=6 时解析 AAAA（显式线路走 resolve_aaaa；系统解析 AF_INET6），
    fake-ip 升级只对 A 记录生效（fake-ip 是 A 应答劫持，不适用于 v6）。
    """
    target = task["target"]
    if is_ip(target):
        return target, None, ""
    host = (url or target) if (url := task.get("_url")) else target
    want_v6 = ip_version == "6"
    if dns_server:
        resolver = resolve_aaaa if want_v6 else resolve_a
        ips, ms, _tr = resolver(host, dns_server, timeout=dns_timeout, cache=cache)
        return ips[0], round(ms, 2), dns_server
    import socket
    t0 = time.monotonic()
    family = socket.AF_INET6 if want_v6 else socket.AF_INET
    infos = socket.getaddrinfo(host, None, family=family)
    ip = str(infos[0][4][0])
    ms = round((time.monotonic() - t0) * 1000, 2)
    if not want_v6 and is_fake_ip(ip):
        # 系统 DNS 被代理 TUN 劫持（返回 fake-ip）：TCP 探测能走代理，但 ICMP（ping/mtr）
        # 打不到 → 改用 DoH 拿真实 IP；DoH 也拿不到时保留 fake-ip，由探测器如实标注
        for srv in ("223.5.5.5", "119.29.29.29", "8.8.8.8"):
            try:
                ips, ms2, _tr = resolve_a(host, srv, timeout=dns_timeout, cache=cache)
                if ips and not is_fake_ip(ips[0]):
                    log.info("系统 DNS 返回 fake-ip(%s)，改用 DoH(%s) 解析 %s → %s",
                             ip, srv, host, ips[0])
                    return ips[0], round(ms2, 2), f"doh:{srv}"
            except Exception:  # noqa: BLE001 - DoH 不可用则继续试下一个
                continue
    return ip, ms, "system"


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        a = cfg.agent
        self.server = a["server_url"].rstrip("/")
        self.name = a.get("name") or (IS_WINDOWS and "win-local" or "linux-local")
        self.data_dir = Path(a["data_dir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cred_file = self.data_dir / "credentials.json"
        self.buffer_file = self.data_dir / "buffer.jsonl"
        self.node_id, self.token = "", ""
        self.tasks: list[dict] = []
        self.config_version = 0
        self.cache = DnsCache(cfg.probe.get("dns_cache_ttl", 60),
                              cfg.probe.get("dns_cache_max", 300))
        self._reg_refreshed = False   # 本进程是否已用新信息注册过
        self.buffer: list[dict] = []
        self.jobs: dict[tuple, dict] = {}       # (task_id,dns,url) -> {interval,next_at,running}
        self.backoff = 1
        self.dns_prev: dict[str, list[str]] = {}  # dns 任务值变更记忆：task_id -> 上次答案集

    # ---------- 凭据 ----------
    def load_creds(self):
        if self.cred_file.exists():
            try:
                d = json.loads(self.cred_file.read_text(encoding="utf-8"))
                self.node_id, self.token = d.get("node_id", ""), d.get("token", "")
                log.info("已载入本地凭据 node_id=%s", self.node_id)
            except Exception as e:  # noqa
                log.warning("凭据文件损坏，将重新注册: %s", e)

    def save_creds(self):
        self.cred_file.write_text(json.dumps(
            {"node_id": self.node_id, "token": self.token}, ensure_ascii=False),
            encoding="utf-8")

    async def ensure_registered(self):
        # 注意：进程启动后要刷新一次注册（幂等）——否则老凭据直接复用，服务端永远拿不到
        # 新的 OS 版本/本机 IP（注册是唯一携带 system 信息的通道）
        if self.node_id and self.token and self._reg_refreshed:
            return
        status, resp = _post_json(f"{self.server}/api/agent/register", {
            "name": self.name, "register_token": self.cfg.agent["register_token"],
            "tags": self.cfg.agent.get("tags") or {}, "version": "0.1.0",
            "system": _system_info(),
            "local_ip": _local_ip(self.server)})
        if status == 200:
            self.node_id, self.token = resp["node_id"], resp.get("token", "")
            # 服务端按名字幂等注册后无独立 token 下发 → token=名字+注册token 哈希约定，
            # Agent 端本地保存等价串由服务端重算，因此这里保存 node_id 即可，
            # token 字段使用 register_token 生成约定串：
            if not self.token:
                import hashlib
                self.token = hashlib.sha256(
                    f"{self.name}:{self.cfg.agent['register_token']}".encode()).hexdigest()
            self.save_creds()
            self.backoff = 1
            self._reg_refreshed = True
            log.info("注册成功 node_id=%s (created=%s) os=%s local_ip=%s",
                     self.node_id, resp.get("created"),
                     _system_info().get("pretty", ""), _local_ip(self.server))
        else:
            raise ConnectionError(f"注册失败 HTTP {status}: {resp.get('detail')}")

    # ---------- 同步 ----------
    async def sync_once(self):
        await self.ensure_registered()
        status, resp = _post_json(f"{self.server}/api/agent/sync", {
            "node_id": self.node_id, "token": self.token,
            "config_version": self.config_version,
            "stats": dict(_system_stats(len(self.jobs)), local_ip=_local_ip(self.server))})
        if status == 401:
            log.warning("凭据失效，重新注册")
            self.node_id = self.token = ""
            return
        if status != 200:
            raise ConnectionError(f"心跳失败 HTTP {status}: {resp.get('detail')}")
        self.backoff = 1
        new_version = resp.get("config_version", 0)
        if new_version != self.config_version and resp.get("tasks") is not None:
            self.apply_tasks(resp["tasks"])
            self.config_version = new_version
            log.info("配置同步 v%s：%d 个任务", new_version, len(self.tasks))

    def apply_tasks(self, tasks: list[dict]):
        probe = self.cfg.probe
        min_i = probe.get("min_interval_seconds", 10)
        min_m = probe.get("min_mtr_interval_seconds", 60)
        min_dns = probe.get("min_dns_interval_seconds", 30)
        jobs = {}
        for t in tasks:
            if not t.get("enabled", 1):
                continue
            if t["type"] == "mtr":
                interval = max(int(t.get("interval_seconds") or 10), min_m)
            elif t["type"] == "dns":
                interval = max(int(t.get("interval_seconds") or 30), min_dns)
            else:
                interval = max(int(t.get("interval_seconds") or 10), min_i)
            # dns 任务是「单流多线路」：任务 dns 字段是要对比的线路列表，不能按线路拆流
            urls = t.get("urls") or [""]
            dnames = [""] if t["type"] == "dns" else (t.get("dns") or [""])
            for d in dnames:
                for u in urls:
                    key = (t["id"], d or "", u or "")
                    old = self.jobs.get(key)
                    jitter = 1 + random.uniform(-probe.get("jitter_ratio", 0.1),
                                                probe.get("jitter_ratio", 0.1))
                    jobs[key] = {
                        "task": t, "dns": d or "", "url": u or "",
                        "interval": interval,
                        "next_at": old["next_at"] if old else now() + random.uniform(0, 3),
                        "running": False,
                    }
        self.tasks = tasks
        self.jobs = jobs
        log.info("配置同步 v%d：%d 个任务 → %d 条结果流（任务×DNS×URL）",
                 self.config_version, len(tasks), len(jobs))

    # ---------- 探测执行 ----------
    def _remember_dns(self, tid: str, answers: list[str]):
        """记录 dns 任务本次答案集（容量上限，插入序淘汰最旧）。"""
        self.dns_prev.pop(tid, None)
        self.dns_prev[tid] = answers
        while len(self.dns_prev) > DNS_MEMORY_MAX:
            self.dns_prev.pop(next(iter(self.dns_prev)))

    async def execute_job(self, key: tuple):
        job = self.jobs[key]
        task, dns, url = job["task"], job["dns"], job["url"]
        ts = now()
        try:
            if task["type"] == "ping":
                r = await asyncio.to_thread(run_ping, task, dns, self.cache, ts,
                                            self.cfg.probe.get("dns_timeout", 2.0))
            elif task["type"] == "curl":
                target_url = url or task["target"]
                ip, dns_ms, dns_label = "", None, ""
                if not is_ip(task["target"]):
                    # URL 里的 host 可能带端口（http://127.0.0.1:8622/x）→ 只解析主机名，
                    # 否则 getaddrinfo("127.0.0.1:8622") 直接失败（浏览器验收实测发现）
                    host = _url_hostname(target_url if "://" in target_url else task["target"])
                    try:
                        ip, dns_ms, dns_label = await asyncio.to_thread(
                            resolve_for, {**task, "_url": host}, dns, self.cache,
                            self.cfg.probe.get("dns_timeout", 2.0),
                            str((task.get("params") or {}).get("ip_version") or "auto"))
                    except DnsError as e:
                        r = {"ts": ts, "status": "fail", "error_class": e.kind,
                             "error": str(e), "dns_server": dns, "resolved_ip": "",
                             "dns_time_ms": None, "metrics": {}}
                    else:
                        r = await asyncio.to_thread(run_curl, task, target_url, dns_label,
                                                    ip, dns_ms, ts)
                else:
                    r = await asyncio.to_thread(run_curl, task, target_url, "", "", None, ts)
            elif task["type"] == "mtr":
                ip, dns_ms, dns_label = "", None, ""
                if not is_ip(task["target"]):
                    try:
                        ip, dns_ms, dns_label = await asyncio.to_thread(
                            resolve_for, task, dns, self.cache,
                            self.cfg.probe.get("dns_timeout", 2.0),
                            str((task.get("params") or {}).get("ip_version") or "auto"))
                    except DnsError as e:
                        r = {"ts": ts, "status": "fail", "error_class": e.kind,
                             "error": str(e), "dns_server": dns, "resolved_ip": "",
                             "dns_time_ms": None, "metrics": {}}
                    else:
                        r = await asyncio.to_thread(run_mtr, task, dns_label, ip, dns_ms, ts)
                else:
                    r = await asyncio.to_thread(run_mtr, task, "", task["target"], None, ts)
            elif task["type"] == "tcp":
                host, _port = split_host_port(task["target"], task.get("params") or {})
                ip, dns_ms, dns_label = "", None, ""
                if not is_ip(host):
                    try:
                        ip, dns_ms, dns_label = await asyncio.to_thread(
                            resolve_for, {**task, "target": host}, dns, self.cache,
                            self.cfg.probe.get("dns_timeout", 2.0))
                    except DnsError as e:
                        r = {"ts": ts, "status": "fail", "error_class": e.kind,
                             "error": str(e), "dns_server": dns, "resolved_ip": "",
                             "dns_time_ms": None, "metrics": {}}
                    else:
                        r = await asyncio.to_thread(run_tcp, task, task["target"], ip,
                                                    dns_label, dns_ms, ts)
                else:
                    r = await asyncio.to_thread(run_tcp, task, task["target"], host, "", None, ts)
            elif task["type"] == "dns":
                # 单流多线路：任务 dns 字段的全部线路一次跑完，进同一结果的 metrics.lines
                prev = self.dns_prev.get(task["id"])
                r = await asyncio.to_thread(
                    run_dnsmon, task, self.cache, ts,
                    self.cfg.probe.get("dns_timeout", 2.0), prev)
                ok_answers = sorted({a for v in (r.get("metrics") or {}).get("lines", {}).values()
                                     if v.get("ok") for a in (v.get("answers") or [])})
                if ok_answers:
                    self._remember_dns(task["id"], ok_answers)
            else:
                return
        except Exception as e:  # noqa
            log.exception("探测执行异常 task=%s: %s", task.get("name"), e)
            r = {"ts": ts, "status": "fail", "error_class": "other", "error": str(e)[:200],
                 "dns_server": dns, "resolved_ip": "", "dns_time_ms": None, "metrics": {}}
        r.update({"task_id": task["id"], "type": task["type"], "node_id": self.node_id,
                  "dns": dns, "url": url, "config_version": self.config_version})
        self.buffer.append(r)
        job["next_at"] = ts + job["interval"]

    # ---------- 上报 ----------
    def _flush_buffer_to_disk(self):
        n = len(self.buffer)
        try:
            with open(self.buffer_file, "a", encoding="utf-8") as f:
                for r in self.buffer:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.buffer = []
            log.warning("上报持续失败，%d 条结果已写入本地缓冲", n)
        except OSError as e:
            log.error("本地缓冲写入失败: %s", e)

    def _load_disk_buffer(self):
        if not self.buffer_file.exists():
            return
        lines = self.buffer_file.read_text(encoding="utf-8").splitlines()
        loaded = 0
        for ln in lines:
            try:
                self.buffer.append(json.loads(ln))
                loaded += 1
            except json.JSONDecodeError:
                continue
        if loaded:
            log.info("从本地缓冲恢复 %d 条结果", loaded)
        self.buffer_file.unlink(missing_ok=True)
        max_b = self.cfg.agent.get("offline_buffer_max", 5000)
        if len(self.buffer) > max_b:
            self.buffer = self.buffer[-max_b:]

    async def report_once(self):
        if not self.buffer or not self.node_id:
            return
        batch = self.buffer[:self.cfg.agent.get("report_batch_size", 50)]
        status, resp = _post_json(f"{self.server}/api/agent/results", {
            "node_id": self.node_id, "token": self.token, "results": batch})
        if status == 200:
            # 只移除服务端确认处理的行（accepted+duplicates）；
            # truncated 部分服务端未处理，留在缓冲队首下轮重发（防丢数据）
            processed = resp.get("accepted", 0) + resp.get("duplicates", 0)
            del self.buffer[:min(len(batch), processed)]
            if resp.get("truncated"):
                log.warning("服务端截断 %d 条，将在下轮重发", resp["truncated"])
            self.backoff = 1
            if resp.get("duplicates"):
                log.debug("去重 %d 条", resp["duplicates"])
        elif status == 401:
            self.node_id = self.token = ""
        else:
            self.backoff = min(self.backoff * 2, 60)
            log.warning("上报失败(HTTP %s)，%.0fs 后重试，缓冲 %d 条", status, self.backoff,
                        len(self.buffer))
            if len(self.buffer) >= self.cfg.agent.get("offline_buffer_max", 5000):
                self._flush_buffer_to_disk()

    # ---------- 主循环 ----------
    async def run(self):
        log.info("Agent 启动 → %s (节点名: %s)", self.server, self.name)
        self.load_creds()
        self._load_disk_buffer()
        hb_i = self.cfg.agent.get("heartbeat_interval", 15)
        poll_i = self.cfg.agent.get("poll_interval", 10)
        rep_i = self.cfg.agent.get("report_interval", 5)
        last_hb = last_rep = 0
        while True:
            t0 = now()
            try:
                if t0 - last_hb >= hb_i or not self.config_version:
                    await self.sync_once()
                    last_hb = t0
            except Exception as e:  # noqa
                self.backoff = min(max(self.backoff * 2, 2), 60)
                log.warning("同步失败(离线运行): %s —— %.0fs 后重试", e, self.backoff)
                await asyncio.sleep(self.backoff)
                continue
            # 调度到期的探测任务
            for key, job in list(self.jobs.items()):
                if not job["running"] and now() >= job["next_at"]:
                    job["running"] = True
                    asyncio.create_task(self._run_job_guarded(key))
                # mtr 执行超间隔的场景：next_at 由执行完成时按 now+interval 推进（跳过积压）
            if t0 - last_rep >= rep_i:
                try:
                    await self.report_once()
                except Exception as e:  # noqa
                    log.warning("上报异常: %s", e)
                last_rep = t0
            await asyncio.sleep(1)

    async def _run_job_guarded(self, key: tuple):
        job = self.jobs.get(key)
        if not job:
            return
        try:
            await self.execute_job(key)
        finally:
            job["running"] = False

    # ---------- 缓冲清理（CLI cache） ----------
    def clear_cache(self):
        self.buffer = []
        self.buffer_file.unlink(missing_ok=True)
        self.cache = DnsCache()
