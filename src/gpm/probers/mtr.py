"""路径探测（mtr 任务）。

- Linux：优先 mtr --json（>=0.94），失败回退 --report 文本。
- Windows：无 mtr，降级用系统 tracert（每跳 3 个探针），跳数与 mtr 同构。
- params.probe_mode=icmp(默认)|tcp|udp：Linux 传 -T/-u（mtr-tiny 支持，通常需 root）；
  Windows tracert 无对应能力 → 非 icmp 模式如实 skipped（unsupported）。
- params.show_asn=true：mtr 加 -z，逐跳解析 AS 号进 hop.asn（文本 "AS<N> " 前缀与
  JSON 的 ASN/asn 字段都兼容）。
- params.ip_version=4|6：mtr/tracert 传 -4/-6（auto 时按目标地址族决定 tracert 的 flag）。
路径故障一律以末跳为准（中间跳丢包多为 ICMP 限速假象）。
工具缺失 / fake-ip 目标 → 如实 skipped（error_class=tool_missing / fake_ip）。
"""
from __future__ import annotations

import json
import re

from ..common.dnsres import is_fake_ip
from ..common.util import IS_WINDOWS, ToolMissing, ToolTimeout, run_cmd
from .base import make_result

_TEXT_HOP = re.compile(
    r"^\s*(\d{1,2})\.\|--\s+(\S+)\s+([\d.]+)%?\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)")
# mtr -z 文本报告的主机列带 "AS<N> " 前缀（未解析时也可能是 "AS??? "）：匹配行首跳号后剥离，
# 让 _TEXT_HOP 继续按单主机列解析
_TEXT_ASN_STRIP = re.compile(r"^(\s*\d{1,2}\.\|--\s+)AS(\S+)\s+")

# ---- Windows tracert 解析（每跳 3 个探针：N ms / <1 ms / *；zh-CN 系统是「毫秒」）----
_TRACERT_HOP = re.compile(r"^\s*(\d{1,2})\s+(.+?)\s*$")
_TRACERT_TOK = re.compile(r"<?\s*(\d+)\s*(?:ms|毫秒)|\*")
_TRACERT_TIMEOUT = re.compile(r"timed out|超时", re.IGNORECASE)

_PROBE_FLAG = {"tcp": "-T", "udp": "-u"}


def _stdev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _parse_tracert(out: str) -> list[dict]:
    """tracert 文本 → hops（与 mtr hops 同构）。

    真实样本（本机 Windows 11 实测）：
      1   288 ms   246 ms   261 ms  8.8.8.8
      1     *        *        *     Request timed out.
    中文系统（zh-CN）的毫秒列是「毫秒」不是 ms，两种写法都要认：
      1    <1 毫秒   <1 毫秒   <1 毫秒 127.0.0.1
    """
    hops = []
    for line in out.splitlines():
        m = _TRACERT_HOP.match(line)
        if not m:
            continue
        rest = m.group(2)
        rtts: list[float] = []
        stars = 0
        for tok in _TRACERT_TOK.finditer(rest):
            if tok.group(0) == "*":
                stars += 1
            else:
                rtts.append(float(tok.group(1)) if tok.group(1) else 1.0)  # <1ms 记 1ms
        probes = len(rtts) + stars
        if probes == 0:
            continue  # 表头 / Trace complete. 之类
        host = _TRACERT_TOK.sub(" ", rest).strip().strip("()")
        if not host or _TRACERT_TIMEOUT.search(host):
            host = "???"
        hops.append({
            "hop": int(m.group(1)), "host": host,
            "loss_pct": round(stars / probes * 100, 1), "snt": probes,
            "last": rtts[-1] if rtts else 0.0,
            "avg": round(sum(rtts) / len(rtts), 3) if rtts else 0.0,
            "best": min(rtts) if rtts else 0.0,
            "wrst": max(rtts) if rtts else 0.0,
            "stdev": round(_stdev(rtts), 3),
        })
    return hops


def _mtr_version() -> tuple[int, int] | None:
    try:
        code, out, _ = run_cmd(["mtr", "--version"], 5)
        m = re.search(r"(\d+)\.(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except ToolMissing:
        return None
    except ToolTimeout:
        return None


def _parse_text_report(out: str, want_asn: bool = False) -> list[dict]:
    hops = []
    for line in out.splitlines():
        asn = None
        work = line
        if want_asn:
            m = _TEXT_ASN_STRIP.match(work)
            if m:
                asn = int(m.group(2)) if m.group(2).isdigit() else None
                work = m.group(1) + work[m.end():]
        mm = _TEXT_HOP.match(work)
        if not mm:
            continue
        hops.append({
            "hop": int(mm.group(1)), "host": mm.group(2), "loss_pct": float(mm.group(3)),
            "snt": int(mm.group(4)), "last": float(mm.group(5)), "avg": float(mm.group(6)),
            "best": float(mm.group(7)), "wrst": float(mm.group(8)), "stdev": float(mm.group(9)),
            "asn": asn,
        })
    return hops


def _hop_asn_from_json(h: dict) -> int | None:
    """mtr --json -z 的 ASN 字段兼容：ASN（可能是数组）/ asn / Fallbacks。"""
    for key in ("ASN", "asn"):
        v = h.get(key)
        if isinstance(v, list):
            nums = [int(x) for x in v if str(x).strip().isdigit()]
            if nums:
                return nums[0]
        elif isinstance(v, int) and v:
            return v
        elif isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


def _parse_json(out: str, want_asn: bool = False) -> list[dict]:
    data = json.loads(out)
    hubs = (data.get("report") or {}).get("hubs") or []
    hops = []
    for i, h in enumerate(hubs, 1):
        host = h.get("host") or "???"
        hops.append({
            "hop": i, "host": host,
            "loss_pct": float(h.get("Loss%", 0) or 0), "snt": int(h.get("Snt", 0) or 0),
            "last": float(h.get("Last", 0) or 0), "avg": float(h.get("Avg", 0) or 0),
            "best": float(h.get("Best", 0) or 0), "wrst": float(h.get("Wrst", 0) or 0),
            "stdev": float(h.get("StDev", 0) or 0),
            "asn": _hop_asn_from_json(h) if want_asn else None,
        })
    return hops


def judge_path(hops: list[dict]) -> tuple[str, str]:
    """末跳为准：末跳 100% 丢包 → path_fail；否则 ok（中间跳丢包仅记录）。

    额外区分「整条路径一个响应都没有」：那是 ICMP 被屏蔽/不可达，不是目标黑洞，
    单独标 path_unreachable，前端会给出「改用 URL/TCP 类任务验证」的提示。
    """
    if not hops:
        return "fail", "mtr_parse_error"
    if all(h["loss_pct"] >= 100.0 for h in hops):
        return "fail", "path_unreachable"
    last = hops[-1]
    if last["loss_pct"] >= 100.0:
        return "fail", "path_fail"
    return "ok", ""


def _tracert_flags(resolved: str, ip_version: str) -> list[str]:
    """tracert 地址族 flag：v6 目标必须 -6；显式 4/6 按指定；auto 维持历史行为 -4。"""
    if ip_version == "6" or (":" in resolved and not resolved.startswith("[")):
        return ["-6"]
    if resolved.startswith("["):
        return ["-6"]
    return ["-4"] if ip_version in ("auto", "4") else ["-6"]


def _run_tracert(task: dict, dns_server: str, resolved_ip: str, dns_time_ms, ts: int) -> dict:
    """Windows 路径探测：tracert -d（不做反向解析）。每跳固定 3 个探针。

    probe_mode=tcp/udp 时 tracert 无对应能力 → 如实 skipped/unsupported（不伪装成 ICMP 结果）。
    """
    params = task.get("params") or {}
    mode = str(params.get("probe_mode") or "icmp").lower()
    if mode != "icmp":
        return make_result(
            ts, "skipped", "unsupported",
            f"Windows tracert 不支持 {mode} 模式（仅 mtr/Linux 支持 -T/-u）；"
            f"该节点此任务按 icmp 无法等价替代，已如实跳过",
            dns_server, resolved_ip, dns_time_ms,
            {"mode": "tracert", "probe_mode": mode, "hops": []})
    # tracert 每跳 3 个探针，最坏耗时 = hops × 3 × probe_ms：
    # 默认 30 跳 × 1s 会到 90s，实测把整条命令拖超时（节点侧如实报 skipped/timeout）。
    # 这里把跳数与单探针等待收到合理范围，并按最坏耗时给命令超时留 1.5 倍余量。
    max_hops = min(int(params.get("max_hops", 30)), 20)
    probe_ms = int(params.get("tracert_probe_ms", 800))
    worst = max_hops * 3 * (probe_ms / 1000.0)
    timeout = max(float(params.get("timeout", 45.0)), 30.0, worst * 1.5)
    target = resolved_ip or task["target"]
    ip_version = str(params.get("ip_version") or "auto")
    try:
        code, out, err = run_cmd(
            ["tracert", "-d", *_tracert_flags(target, ip_version),
             "-h", str(max_hops), "-w", str(probe_ms), target], timeout)
    except ToolMissing as e:
        return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
    except ToolTimeout as e:
        return make_result(ts, "skipped", "timeout", str(e), dns_server, resolved_ip, dns_time_ms)

    hops = _parse_tracert(out)
    status, err_cls = judge_path(hops)
    metrics = {"mode": "tracert", "probes_per_hop": 3, "probe_mode": "icmp", "hops": hops}
    if status == "ok":
        return make_result(ts, "ok", "", "", dns_server, target, dns_time_ms, metrics)
    return make_result(ts, "fail", err_cls, "末跳全部超时或无有效跳",
                       dns_server, target, dns_time_ms, metrics)


def run_mtr(task: dict, dns_server: str, resolved_ip: str, dns_time_ms, ts: int) -> dict:
    if is_fake_ip(resolved_ip):
        # 解析到 fake-ip（代理 TUN 劫持 DNS）：ICMP 打不到，直接如实跳过而不是白等超时
        return make_result(ts, "skipped", "fake_ip",
                           f"解析到 fake-ip({resolved_ip})，ICMP 无法探测；请为该任务指定 DNS 线路"
                           f"或改用 IP 目标", dns_server, resolved_ip, dns_time_ms)
    if IS_WINDOWS:
        # Windows 无 mtr：用系统 tracert 降级出跳数（每跳 3 个探针，粒度比 mtr 粗）
        return _run_tracert(task, dns_server, resolved_ip, dns_time_ms, ts)
    params = task.get("params") or {}
    cycles = int(params.get("cycles", 10))
    max_hops = int(params.get("max_hops", 30))
    mode = str(params.get("probe_mode") or "icmp").lower()
    show_asn = bool(params.get("show_asn"))
    ip_version = str(params.get("ip_version") or "auto")
    # params.timeout 在 UI 里是「单次探测超时」（curl 10s），对 mtr 是「整条命令超时」：
    # 直接套用会把 10 个周期的 mtr 在 10s 时杀掉（实测 mtr-jd.com 全部 timeout），
    # 因此按 cycles 给足下限
    timeout = max(float(params.get("timeout", 45.0)), 20.0, cycles * 2.5)

    ver = _mtr_version()
    if ver is None:
        return make_result(ts, "skipped", "tool_missing", "mtr 未安装", dns_server, resolved_ip, dns_time_ms)

    target = resolved_ip or task["target"]
    # 地址族 flag：显式 4/6 按指定；auto 不传（mtr 按目标自行选择）
    fam = ["-4"] if ip_version == "4" else (["-6"] if ip_version == "6" else [])
    probe_flag = [_PROBE_FLAG[mode]] if mode in _PROBE_FLAG else []
    asn_flag = ["-z"] if show_asn else []
    use_json = ver >= (0, 94)  # 0.94+ 曾有回归，0.93~0.94 边界择稳：>=0.94 用 JSON，失败回退文本
    try:
        if use_json:
            code, out, err = run_cmd(["mtr", "--json", *fam, *probe_flag, *asn_flag,
                                      "-c", str(cycles), "-n", "-m",
                                      str(max_hops), target], timeout)
            hops = _parse_json(out, show_asn) if code == 0 and out.strip().startswith("{") else []
        else:
            hops = []
        if not hops:
            code, out, err = run_cmd(["mtr", "--report", *fam, *probe_flag, *asn_flag,
                                      "--report-cycles", str(cycles),
                                      "-n", "-m", str(max_hops), target], timeout)
            hops = _parse_text_report(out, show_asn)
    except ToolMissing as e:
        return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
    except ToolTimeout as e:
        return make_result(ts, "skipped", "timeout", str(e), dns_server, resolved_ip, dns_time_ms)

    if not hops and mode != "icmp" and re.search(
            r"root|permission|capabilit", (err or ""), re.IGNORECASE):
        # mtr -T/-u 需要 root/CAP_NET_RAW（mtr-ping 小能力集）：缺权限是环境问题，
        # 如实 skipped 而不是伪装成 mtr_parse_error
        return make_result(ts, "skipped", "unsupported",
                           f"mtr {mode} 模式需要 root/CAP_NET_RAW：{(err or '').strip()[:150]}",
                           dns_server, resolved_ip, dns_time_ms,
                           {"cycles": cycles, "hops": [], "json_mode": use_json,
                            "probe_mode": mode, "show_asn": show_asn})

    status, err_cls = judge_path(hops)
    metrics = {"cycles": cycles, "hops": hops, "json_mode": use_json,
               "probe_mode": mode, "show_asn": show_asn}
    if status == "ok":
        return make_result(ts, "ok", "", "", dns_server, target, dns_time_ms, metrics)
    return make_result(ts, "fail", err_cls, "末跳 100% 丢包或无有效跳",
                       dns_server, target, dns_time_ms, metrics)
