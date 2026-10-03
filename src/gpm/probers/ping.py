"""ping 探测：先解析后探测（对解析到的 IP 执行），系统 ping，双平台双语言解析。

params.ip_version ∈ auto|4|6：auto 维持原行为（解析 A / getaddrinfo AF_INET）；
6 时解析 AAAA（显式线路走 dnsres.resolve_aaaa，系统解析走 AF_INET6）并给 ping 传 -6。
"""
from __future__ import annotations

from ..common.dnsres import DnsError, resolve_a, resolve_aaaa
from ..common.util import is_ip, run_cmd, ToolMissing, ToolTimeout, validate_target
from .base import make_result, parse_ping, ping_cmd


def run_ping(task: dict, dns_server: str, cache, ts: int,
             dns_timeout: float = 2.0) -> dict:
    """task: {target, params{count,timeout,ip_version}}。dns_server='' 表示节点默认解析（系统解析）。"""
    target, params = task["target"], task.get("params") or {}
    count = int(params.get("count", 4))
    timeout = float(params.get("timeout", 2.0))
    ip_version = str(params.get("ip_version") or "auto")
    want_v6 = ip_version == "6"
    dns_server = dns_server or ""
    resolved_ip, dns_time_ms = "", None

    transport = None
    try:
        if is_ip(target):
            resolved_ip = target
        elif not dns_server:
            # 节点默认解析：系统 getaddrinfo
            import socket
            import time as _t
            t0 = _t.monotonic()
            family = socket.AF_INET6 if want_v6 else socket.AF_INET
            infos = socket.getaddrinfo(target, None, family=family)
            resolved_ip = str(infos[0][4][0])
            dns_time_ms = round((_t.monotonic() - t0) * 1000, 2)
            dns_server = "system"
        else:
            resolver = resolve_aaaa if want_v6 else resolve_a
            ips, ms, transport = resolver(target, dns_server, timeout=dns_timeout, cache=cache)
            resolved_ip, dns_time_ms = ips[0], round(ms, 2)
    except DnsError as e:
        return make_result(ts, "fail", e.kind, str(e), dns_server=dns_server or "", dns_time_ms=None)
    except OSError as e:
        return make_result(ts, "fail", "dns_error", f"默认解析失败: {e}", dns_server="system")

    # 字面量/解析结果的地址族决定 fallback：auto 且目标是 v6 时也带 -6，避免 Windows 默认 v4
    fam = ip_version
    if fam == "auto" and ":" in resolved_ip and validate_target(resolved_ip):
        fam = "6"
    try:
        code, out, err = run_cmd(ping_cmd(resolved_ip, count, timeout, fam), timeout * count + 8)
    except ToolMissing as e:
        return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
    except ToolTimeout as e:
        return make_result(ts, "fail", "timeout", str(e), dns_server, resolved_ip, dns_time_ms)

    p = parse_ping(out, count)
    if transport:
        p["resolve_transport"] = transport
    if p["received"] == 0:
        return make_result(ts, "fail", p["error_class"] or "timeout",
                           p["error_class"] or "100% 丢包", dns_server, resolved_ip, dns_time_ms, p)
    return make_result(ts, "ok", "", "", dns_server, resolved_ip, dns_time_ms, p)
