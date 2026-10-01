"""curl 探测：-w 8 字段数值解析，期望状态码（对齐 blackbox_exporter valid_status_codes 语义）。"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..common.util import IS_WINDOWS, run_cmd, ToolMissing, ToolTimeout
from .base import make_result

_CURL_EXIT = {6: "dns_error", 7: "connect_timeout", 28: "response_timeout",
              35: "tls_error", 56: "recv_error", 60: "tls_error"}


def _in_expected(code: int, expected) -> bool:
    for item in expected or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            if item[0] <= code < item[1]:
                return True
        elif isinstance(item, int) and item == code:
            return True
    return False


def curl_cmd(url: str, timeout: float, resolved_ip: str = "", follow: bool = True) -> list[str]:
    fmt = "%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} " \
          "%{time_starttransfer} %{time_total} %{size_download} %{remote_ip}"
    args = ["curl", "-sS", "-o", "NUL" if IS_WINDOWS else "/dev/null",
            "--max-time", str(timeout), "-w", fmt]
    if IS_WINDOWS:
        args.append("--ssl-no-revoke")   # Schannel 吊销检查在受限网络下误报
    if follow:
        args.append("-L")
    if resolved_ip:
        p = urlparse(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        args += ["--resolve", f"{p.hostname}:{port}:{resolved_ip}"]
    args.append(url)
    return args


def run_curl(task: dict, url: str, dns_server: str, resolved_ip: str,
             dns_time_ms, ts: int) -> dict:
    params = task.get("params") or {}
    timeout = float(params.get("timeout", 10.0))
    expected = params.get("expected_status", [[200, 300]])
    follow = bool(params.get("follow_redirects", True))
    try:
        code, out, err = run_cmd(curl_cmd(url, timeout, resolved_ip if dns_server else ""),
                                 timeout + 8)
    except ToolMissing as e:
        return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
    except ToolTimeout as e:
        return make_result(ts, "fail", "response_timeout", str(e), dns_server, resolved_ip, dns_time_ms)

    nums = re.findall(r"\S+", out.strip())
    if len(nums) < 8 or not re.match(r"^\d{3}$", nums[0]):
        cls = _CURL_EXIT.get(code, "other")
        detail = (err or out).strip()[:200] or f"curl exit={code}"
        return make_result(ts, "fail", cls, detail, dns_server, resolved_ip, dns_time_ms)
    http_code = int(nums[0])
    m = {
        "http_code": http_code,
        "dns_time": round(float(nums[1]) * 1000, 2),
        "connect_time": round(float(nums[2]) * 1000, 2),
        "tls_time": round(float(nums[3]) * 1000, 2),
        "ttfb": round(float(nums[4]) * 1000, 2),
        "total_time": round(float(nums[5]) * 1000, 2),
        "size": int(float(nums[6])),
        "remote_ip": nums[7],
        "http_reached": True,     # 有 HTTP 响应（网络可达）
    }
    ok = _in_expected(http_code, expected)
    if not ok:
        return make_result(ts, "fail", f"http_{http_code}",
                           f"状态码 {http_code} 不在期望集合", dns_server,
                           m["remote_ip"] or resolved_ip, dns_time_ms, m)
    return make_result(ts, "ok", "", "", dns_server, m["remote_ip"] or resolved_ip, dns_time_ms, m)
