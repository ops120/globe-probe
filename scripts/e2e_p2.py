"""P2 端到端真实数据实测（一次性实例，仿 e2e_real.py）：

启动独立 gpm server + agent（端口 8625 / 独立临时库）→ 通过 API 创建三类新任务
（TCP 端口 / DNS 解析 / curl 关键字增强）→ 等待 ≥2 个探测周期 → API 验收 → 清理。

用法：python scripts/e2e_p2.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8625
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e-p2-data"

FAKE_PREFIXES = ("198.18.", "198.19.")


def api(path, method="GET", body=None, expect_error=False):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if expect_error:
            return {"status": e.code}
        raise


def write_configs():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "agent").mkdir(exist_ok=True)
    db = (DATA / "e2e-p2.db").as_posix()
    adir = (DATA / "agent").as_posix()
    (DATA / "server.yaml").write_text(
        'server:\n'
        f'  listen: "127.0.0.1:{PORT}"\n'
        f'  database: "{db}"\n'
        'agent:\n'
        '  register_token: "e2e-p2-token"\n'
        'logging:\n  level: "INFO"\n', encoding="utf-8")
    (DATA / "agent.yaml").write_text(
        'agent:\n'
        f'  server_url: "{BASE}"\n'
        '  register_token: "e2e-p2-token"\n'
        '  name: "e2e-p2-node"\n'
        f'  data_dir: "{adir}"\n'
        '  heartbeat_interval: 5\n'
        '  report_interval: 3\n'
        'probe:\n'
        '  dns_timeout: 6.0\n', encoding="utf-8")


def main():
    checks = []
    write_configs()
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    log_s = open(DATA / "server.log", "w", encoding="utf-8")
    log_a = open(DATA / "agent.log", "w", encoding="utf-8")
    procs = []
    try:
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "gpm", "--config", str(DATA / "server.yaml"), "server"],
            cwd=ROOT, env=env, stdout=log_s, stderr=subprocess.STDOUT))
        for _ in range(30):
            time.sleep(1)
            try:
                api("/api/health")
                break
            except Exception:
                continue
        print(f"[1] 一次性服务端启动 OK (端口 {PORT})")
        checks.append(("服务端启动", True, f"端口 {PORT}"))

        procs.append(subprocess.Popen(
            [sys.executable, "-m", "gpm", "--config", str(DATA / "agent.yaml"), "agent"],
            cwd=ROOT, env=env, stdout=log_a, stderr=subprocess.STDOUT))
        time.sleep(8)
        nodes = api("/api/nodes")
        assert any(n["status"] == "online" for n in nodes), "节点未上线"
        node_id = [n for n in nodes if n["status"] == "online"][0]["id"]
        print(f"[2] 节点注册并在线: {node_id}")
        checks.append(("节点注册/心跳", True, node_id))

        t_tcp = api("/api/tasks", "POST", {
            "name": "e2e2-tcp-loop", "type": "tcp", "target": f"127.0.0.1:{PORT}",
            "interval_seconds": 10, "params": {"timeout": 3}})
        t_dns = api("/api/tasks", "POST", {
            "name": "e2e2-dns-example", "type": "dns", "target": "example.com",
            "dns": ["udp:223.5.5.5"], "interval_seconds": 30, "params": {}})
        t_curl = api("/api/tasks", "POST", {
            "name": "e2e2-curl-health", "type": "curl", "target": "127.0.0.1",
            "urls": [f"{BASE}/api/health"], "interval_seconds": 10,
            "params": {"timeout": 5, "method": "GET",
                       "headers": {"X-Probe": "gpm-e2e"}, "keyword": "ok"}})
        print(f"[3] 任务创建: tcp={t_tcp['id'][:9]}… dns={t_dns['id'][:9]}… curl={t_curl['id'][:9]}…")
        checks.append(("tcp/dns/curl 任务创建", True, ""))

        print("[4] 真实探测运行 75s（≥2 个 dns 周期）…")
        time.sleep(75)

        def results(tid):
            rows = api(f"/api/export?task_id={tid}&fmt=json")
            return [r for r in rows if r["status"] != "skipped"]

        def latest_ok(rows, rtype):
            ok = [r for r in rows if r["status"] == "ok" and r["type"] == rtype]
            return ok[-1] if ok else None

        # ---- tcp ----
        tcp_rows = results(t_tcp["id"])
        tcp = latest_ok(tcp_rows, "tcp")
        checks.append(("tcp 有成功结果", bool(tcp), f"{len(tcp_rows)} 条结果"))
        if tcp:
            m = tcp["metrics"]
            checks.append(("tcp metrics 形状 (rtt_ms>0/port)",
                           m.get("rtt_ms", 0) > 0 and m.get("port") == PORT,
                           f"rtt_ms={m.get('rtt_ms')} port={m.get('port')}"))
        # rtt 聚合可用（raw series 取得到 rtt）
        sv = api(f"/api/query/series?task_id={t_tcp['id']}&node_id={node_id}"
                 f"&metric=rtt&granularity=raw")
        ok_pts = [p for p in sv["points"] if p["status"] == "ok" and p["v"]]
        checks.append(("tcp rtt 进入查询/聚合通道", len(ok_pts) >= 1, f"{len(ok_pts)} 点"))

        # ---- dns ----
        dns_rows = results(t_dns["id"])
        dns = latest_ok(dns_rows, "dns")
        checks.append(("dns 有成功结果", bool(dns), f"{len(dns_rows)} 条结果"))
        if dns:
            m = dns["metrics"]
            line = (m.get("lines") or {}).get("udp:223.5.5.5") or {}
            answers = line.get("answers") or []
            real = bool(answers) and not any(a.startswith(FAKE_PREFIXES) for a in answers)
            checks.append(("dns 拿到真实非 fake-ip 答案", real,
                           f"answers={answers[:3]} upgraded={line.get('fake_ip_upgraded', False)}"))
            checks.append(("dns metrics 形状 (lines/consistent/rtt_ms)",
                           "lines" in m and isinstance(m.get("consistent"), bool)
                           and m.get("rtt_ms") is not None,
                           f"consistent={m.get('consistent')} rtt_ms={m.get('rtt_ms')}"))
            checks.append(("dns ttl/ms 齐全", line.get("ttl") is not None and line.get("ms") is not None,
                           f"ttl={line.get('ttl')} ms={line.get('ms')}"))

        # ---- curl 增强 ----
        curl_rows = results(t_curl["id"])
        curl = latest_ok(curl_rows, "curl")
        checks.append(("curl 有成功结果", bool(curl), f"{len(curl_rows)} 条结果"))
        if curl:
            m = curl["metrics"]
            checks.append(("curl 状态码/关键字命中", m.get("http_code") == 200
                           and m.get("keyword_hit") is True,
                           f"http={m.get('http_code')} kw={m.get('keyword_hit')}"))

        print("\n===== P2 E2E 验收结果 =====")
        passed = 0
        for name, ok, detail in checks:
            print(f" [{'v' if ok else 'x'}] {name:36s} {detail}")
            passed += bool(ok)
        print(f"===== {passed}/{len(checks)} 通过 =====")

        # 输出三任务的最近一条原始结果（JSON 片段，供报告引用）
        print("\n----- 原始结果样例 -----")
        for label, rows in (("tcp", tcp_rows), ("dns", dns_rows), ("curl", curl_rows)):
            r = latest_ok(rows, label)
            if r:
                slim = {k: r[k] for k in ("ts", "type", "status", "task_id") if k in r}
                slim["metrics"] = r.get("metrics")
                print(f"{label}: " + json.dumps(slim, ensure_ascii=False)[:600])
        return 0 if passed == len(checks) else 1
    finally:
        for p in procs:
            p.terminate()
        time.sleep(1)
        for p in procs:
            if p.poll() is None:
                p.kill()
        for f in (log_s, log_a):
            f.close()
        shutil.rmtree(DATA, ignore_errors=True)   # 清理临时目录（含库/凭据/日志）
        print("\n一次性实例已停止，临时目录已清理")


if __name__ == "__main__":
    sys.exit(main())
