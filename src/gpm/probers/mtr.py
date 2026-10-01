"""路径探测（mtr 任务）。

- Linux：优先 mtr --json（>=0.94），失败回退 --report 文本。
- Windows：无 mtr，降级用系统 tracert（每跳 3 个探针），跳数与 mtr 同构。
路径故障一律以末跳为准（中间跳丢包多为 ICMP 限速假象）。
工具缺失 / fake-ip 目标 → 如实 skipped（error_class=tool_missing / fake_ip）。
"""
from __future__ import annotations

import json
import re

from ..common.dnsres import is_fake_ip
from ..common.util import IS_WINDOWS, run_cmd, ToolMissing, ToolTimeout
from .base import make_result

_TEXT_HOP = re.compile(
    r"^\s*(\d{1,2})\.\|--\s+(\S+)\s+([\d.]+)%?\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)")


# ---- Windows tracert 解析（每跳 3 个探针：N ms / <1 ms / *）----
_TRACERT_HOP = re.compile(r"^\s*(\d{1,2})\s+(.+?)\s*$")
_TRACERT_TOK = re.compile(r"<?\s*(\d+)\s*ms|\*")
_TRACERT_TIMEOUT = re.compile(r"timed out|超时", re.IGNORECASE)


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


def _parse_text_report(out: str) -> list[dict]:
    hops = []
    for line in out.splitlines():
        m = _TEXT_HOP.match(line)
        if not m:
            continue
        hops.append({
            "hop": int(m.group(1)), "host": m.group(2), "loss_pct": float(m.group(3)),
            "snt": int(m.group(4)), "last": float(m.group(5)), "avg": float(m.group(6)),
            "best": float(m.group(7)), "wrst": float(m.group(8)), "stdev": float(m.group(9)),
        })
    return hops


def _parse_json(out: str) -> list[dict]:
    data = json.loads(out)
    hubs = (data.get("report") or {}).get("hubs") or []
    hops = []
    for i, h in enumerate(hubs, 1):
        hops.append({
            "hop": i, "host": h.get("host") or "???",
            "loss_pct": float(h.get("Loss%", 0) or 0), "snt": int(h.get("Snt", 0) or 0),
            "last": float(h.get("Last", 0) or 0), "avg": float(h.get("Avg", 0) or 0),
            "best": float(h.get("Best", 0) or 0), "wrst": float(h.get("Wrst", 0) or 0),
            "stdev": float(h.get("StDev", 0) or 0),
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


def _run_tracert(task: dict, dns_server: str, resolved_ip: str, dns_time_ms, ts: int) -> dict:
    """Windows 路径探测：tracert -d（不做反向解析）。每跳固定 3 个探针。"""
    params = task.get("params") or {}
    # tracert 每跳 3 个探针，最坏耗时 = hops × 3 × probe_ms：
    # 默认 30 跳 × 1s 会到 90s，实测把整条命令拖超时（节点侧如实报 skipped/timeout）。
    # 这里把跳数与单探针等待收到合理范围，并按最坏耗时给命令超时留 1.5 倍余量。
    max_hops = min(int(params.get("max_hops", 30)), 20)
    probe_ms = int(params.get("tracert_probe_ms", 800))
    worst = max_hops * 3 * (probe_ms / 1000.0)
    timeout = max(float(params.get("timeout", 45.0)), 30.0, worst * 1.5)
    target = resolved_ip or task["target"]
    try:
        code, out, err = run_cmd(
            ["tracert", "-d", "-4", "-h", str(max_hops), "-w", str(probe_ms), target], timeout)
    except ToolMissing as e:
        return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
    except ToolTimeout as e:
        return make_result(ts, "skipped", "timeout", str(e), dns_server, resolved_ip, dns_time_ms)

    hops = _parse_tracert(out)
    status, err_cls = judge_path(hops)
    metrics = {"mode": "tracert", "probes_per_hop": 3, "hops": hops}
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
    # params.timeout 在 UI 里是「单次探测超时」（curl 10s），对 mtr 是「整条命令超时」：
    # 直接套用会把 10 个周期的 mtr 在 10s 时杀掉（实测 mtr-jd.com 全部 timeout），
    # 因此按 cycles 给足下限
    timeout = max(float(params.get("timeout", 45.0)), 20.0, cycles * 2.5)

    ver = _mtr_version()
    if ver is None:
        return make_result(ts, "skipped", "tool_missing", "mtr 未安装", dns_server, resolved_ip, dns_time_ms)

    target = resolved_ip or task["target"]
    use_json = ver >= (0, 94)  # 0.94+ 曾有回归，0.93~0.94 边界择稳：>=0.94 用 JSON，失败回退文本
    try:
        if use_json:
            code, out, err = run_cmd(["mtr", "--json", "-c", str(cycles), "-n", "-m",
                                      str(max_hops), target], timeout)
            hops = _parse_json(out) if code == 0 and out.strip().startswith("{") else []
        else:
            hops = []
        if not hops:
            code, out, err = run_cmd(["mtr", "--report", "--report-cycles", str(cycles),
                                      "-n", "-m", str(max_hops), target], timeout)
            hops = _parse_text_report(out)
    except ToolMissing as e:
        return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
    except ToolTimeout as e:
        return make_result(ts, "skipped", "timeout", str(e), dns_server, resolved_ip, dns_time_ms)

    status, err_cls = judge_path(hops)
    metrics = {"cycles": cycles, "hops": hops, "json_mode": use_json}
    if status == "ok":
        return make_result(ts, "ok", "", "", dns_server, target, dns_time_ms, metrics)
    return make_result(ts, "fail", err_cls, "末跳 100% 丢包或无有效跳",
                       dns_server, target, dns_time_ms, metrics)
