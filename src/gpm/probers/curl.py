"""curl 探测：-w 8 字段数值解析，期望状态码（对齐 blackbox_exporter valid_status_codes 语义）。

P2 增强（全部可选，缺省维持原行为）：
  params.method            GET/HEAD/POST/PUT/DELETE/PATCH/OPTIONS（HEAD 用 -I，其余 -X）
  params.headers           扁平 str→str dict（≤10 条，模型层校验），逐条 -H
  params.body              请求体（≤8KB），--data-binary（配合 -X 保持语义）
  params.follow_redirects  false 时不加 -L（缺省跟随，维持原行为）
  params.keyword / regex   响应体子串命中 / 正则 search；未命中 → fail keyword_miss / regex_miss
  params.cert_check        https 时用 python ssl 直连取证书 notAfter → cert_days/cert_not_after
                           （解析失败如实跳过 cert 指标，不判失败）；cert_min_days 低于阈值 →
                           fail/cert_expired（设置 cert_min_days 等价于打开 cert_check）
keyword/regex 需要响应体：此时下载到临时文件（只读前 512KB 扫描），否则维持 -o NUL。
"""
from __future__ import annotations

import os
import re
import socket
import ssl
import tempfile
import time
from urllib.parse import urlparse

from ..common.util import IS_WINDOWS, run_cmd, ToolMissing, ToolTimeout
from .base import make_result

_CURL_EXIT = {6: "dns_error", 7: "connect_timeout", 28: "response_timeout",
              35: "tls_error", 56: "recv_error", 60: "tls_error"}

SCAN_LIMIT = 512 * 1024   # keyword/regex 只扫响应体前 512KB（防大响应拖垮内存）


def _in_expected(code: int, expected) -> bool:
    for item in expected or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            if item[0] <= code < item[1]:
                return True
        elif isinstance(item, int) and item == code:
            return True
    return False


def curl_cmd(url: str, timeout: float, resolved_ip: str = "", follow: bool = True,
             method: str = "", headers: list[str] | None = None,
             body: str = "", body_out: str = "") -> list[str]:
    fmt = "%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} " \
          "%{time_starttransfer} %{time_total} %{size_download} %{remote_ip}"
    args = ["curl", "-sS",
            "-o", body_out or ("NUL" if IS_WINDOWS else "/dev/null"),
            "--max-time", str(timeout), "-w", fmt]
    if IS_WINDOWS:
        args.append("--ssl-no-revoke")   # Schannel 吊销检查在受限网络下误报
    if follow:
        args.append("-L")
    m = (method or "GET").upper()
    if m == "HEAD":
        args.append("-I")
    elif m != "GET":
        args += ["-X", m]
    for h in headers or []:
        args += ["-H", h]
    if body:
        args += ["--data-binary", body]
    if resolved_ip:
        p = urlparse(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        args += ["--resolve", f"{p.hostname}:{port}:{resolved_ip}"]
    args.append(url)
    return args


def parse_curl_metrics(out: str) -> dict | None:
    """解析 curl -w 输出，返回阶段耗时（ms，保留 2 位小数）。无法解析时返回 None。

    说明：curl 没有单独的「下载耗时」变量，这里用 total − starttransfer 的差值，
    并在**原始秒值**上相减后再取整（先各自 round 再相减会产生 0.09000000000000341 这类浮点噪声）。
    """
    nums = re.findall(r"\S+", (out or "").strip())
    if len(nums) < 8 or not re.match(r"^\d{3}$", nums[0]):
        return None
    try:
        total_s, ttfb_s = float(nums[5]), float(nums[4])
    except ValueError:
        return None
    return {
        "http_code": int(nums[0]),
        "dns_time": round(float(nums[1]) * 1000, 2),
        "connect_time": round(float(nums[2]) * 1000, 2),
        "tls_time": round(float(nums[3]) * 1000, 2),
        "ttfb": round(ttfb_s * 1000, 2),
        "total_time": round(total_s * 1000, 2),
        "download_time": round(max(0.0, total_s - ttfb_s) * 1000, 2),
        "size": int(float(nums[6])),
        "remote_ip": nums[7],
        "http_reached": True,     # 有 HTTP 响应（网络可达）
    }


def _cert_not_after_epoch(s: str) -> int | None:
    import calendar
    m = re.match(r"^([A-Za-z]{3}) +(\d{1,2}) +(\d{2}:\d{2}:\d{2}) +(\d{4}) GMT$", (s or "").strip())
    if not m:
        return None
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    if m.group(1) not in months:
        return None
    hh, mm, ss = (int(x) for x in m.group(3).split(":"))
    try:
        return calendar.timegm((int(m.group(4)), months[m.group(1)], int(m.group(2)),
                                hh, mm, ss, 0, 0, 0))
    except (ValueError, OverflowError):
        return None


def fetch_cert_days(url: str, timeout: float = 5.0) -> tuple[int | None, int | None]:
    """python ssl 直连取 https 站点证书余量 → (cert_days, not_after_epoch)。

    直连失败（网络/握手/SNI）如实返回 (None, None)：cert 指标跳过，不影响探测结论。
    """
    p = urlparse(url)
    if p.scheme != "https":
        return None, None
    host, port = p.hostname or "", p.port or 443
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
        na = _cert_not_after_epoch(str(cert.get("notAfter") or ""))
        if na is None:
            return None, None
        return int((na - time.time()) // 86400), na
    except (OSError, ssl.SSLError, ValueError):
        return None, None


def _scan_body(path: str, keyword: str, regex: str) -> tuple[bool, bool, str]:
    """读临时文件前 SCAN_LIMIT 字节，返回 (keyword_hit, regex_hit, 读到的文本)。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(SCAN_LIMIT)
    except OSError:
        return False, False, ""
    text = raw.decode("utf-8", errors="replace")
    kw_hit = bool(keyword) and keyword in text
    rx_hit = False
    if regex:
        try:
            rx_hit = re.search(regex, text) is not None
        except re.error:
            rx_hit = False
    return kw_hit, rx_hit, text


def run_curl(task: dict, url: str, dns_server: str, resolved_ip: str,
             dns_time_ms, ts: int) -> dict:
    params = task.get("params") or {}
    timeout = float(params.get("timeout", 10.0))
    expected = params.get("expected_status", [[200, 300]])
    follow = bool(params.get("follow_redirects", True))
    method = str(params.get("method") or "GET").upper()
    headers = [f"{k}: {v}" for k, v in (params.get("headers") or {}).items()]
    body = str(params.get("body") or "")
    keyword = str(params.get("keyword") or "")
    regex = str(params.get("regex") or "")
    need_body = bool(keyword or regex)
    cert_min_days = params.get("cert_min_days")
    do_cert = bool(params.get("cert_check")) or cert_min_days is not None

    body_out = ""
    tmp_cleanup = None
    if need_body:
        fd, body_out = tempfile.mkstemp(prefix="gpm-body-", suffix=".out")
        os.close(fd)
        tmp_cleanup = body_out
    try:
        try:
            code, out, err = run_cmd(
                curl_cmd(url, timeout, resolved_ip if dns_server else "", follow,
                         method, headers, body, body_out),
                timeout + 8)
        except ToolMissing as e:
            return make_result(ts, "skipped", "tool_missing", str(e), dns_server, resolved_ip, dns_time_ms)
        except ToolTimeout as e:
            return make_result(ts, "fail", "response_timeout", str(e), dns_server, resolved_ip, dns_time_ms)

        m = parse_curl_metrics(out)
        if m is None:
            cls = _CURL_EXIT.get(code, "other")
            detail = (err or out).strip()[:200] or f"curl exit={code}"
            return make_result(ts, "fail", cls, detail, dns_server, resolved_ip, dns_time_ms)
        http_code = m["http_code"]
        ok = _in_expected(http_code, expected)
        if not ok:
            return make_result(ts, "fail", f"http_{http_code}",
                               f"状态码 {http_code} 不在期望集合", dns_server,
                               m["remote_ip"] or resolved_ip, dns_time_ms, m)
        if need_body:
            kw_hit, rx_hit, _text = _scan_body(body_out, keyword, regex)
            m["keyword_hit"] = kw_hit
            m["regex_hit"] = rx_hit
            if keyword and not kw_hit:
                return make_result(ts, "fail", "keyword_miss",
                                   f"响应体未包含关键字: {keyword[:60]}",
                                   dns_server, m["remote_ip"] or resolved_ip, dns_time_ms, m)
            if regex and not rx_hit:
                return make_result(ts, "fail", "regex_miss",
                                   f"响应体未匹配正则: {regex[:60]}",
                                   dns_server, m["remote_ip"] or resolved_ip, dns_time_ms, m)
        if do_cert:
            days, na = fetch_cert_days(url, min(timeout, 8.0))
            if days is not None:
                m["cert_days"] = days
                m["cert_not_after"] = na
            if cert_min_days is not None and days is not None and days < int(cert_min_days):
                return make_result(ts, "fail", "cert_expired",
                                   f"证书仅剩 {days} 天（阈值 {cert_min_days} 天）",
                                   dns_server, m["remote_ip"] or resolved_ip, dns_time_ms, m)
        return make_result(ts, "ok", "", "", dns_server, m["remote_ip"] or resolved_ip, dns_time_ms, m)
    finally:
        if tmp_cleanup:
            try:
                os.unlink(tmp_cleanup)
            except OSError:
                pass
