"""TCP 端口连通性探测：socket 建连耗时 + 可选 TLS 证书余量。

- 目标 host:port（params.port 可拆分：target 只写 host 时用 params.port）。
- metrics：rtt_ms=connect 耗时（同时写 rtt_avg，复用现有 rtt 聚合/告警规则）、
  tls=true 时附带 cert_days / cert_not_after（证书链不校验——那是 curl 的职责，
  这里只关心「端口可达 + 证书还剩几天」，自签站也能拿到 notAfter）。
- error_class：dns_error（解析失败在 agent 侧标注）/ timeout / refused / tls_error / other。
每任务单流（url=""、dns=""），agent 先解析后探测（与 curl/mtr 同构）。
"""
from __future__ import annotations

import os
import re
import socket
import ssl
import time

from .base import make_result

_CERT_NOTAFTER_RE = re.compile(r"^([A-Za-z]{3}) +(\d{1,2}) +(\d{2}:\d{2}:\d{2}) +(\d{4}) GMT$")
_CERT_MONTH = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def split_host_port(target: str, params: dict) -> tuple[str, int | None]:
    """把 tcp 任务目标拆成 (host, port)。

    支持 host:port / [v6]:port / [v6] / 裸 host / 裸 v6；无端口时回退 params.port。
    非法端口按「无端口」处理（由上层报缺少端口）。
    """
    target = (target or "").strip()
    params = params or {}
    host, port = target, None
    if target.startswith("["):                       # [v6] 或 [v6]:port
        bracket, sep, rest = target.partition("]")
        host = bracket[1:]
        if sep and rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif target.count(":") == 1:                     # host:port（v6 裸串多冒号不在此列）
        h, _, p = target.partition(":")
        host = h
        if p.isdigit():
            port = int(p)
    if port is None and params.get("port"):
        try:
            port = int(params["port"])
        except (TypeError, ValueError):
            port = None
    return host, port


def _parse_not_after(s: str) -> int | None:
    """ssl 证书 notAfter（如 'Nov 28 12:00:00 2026 GMT'）→ epoch 秒；解析失败 None。"""
    import calendar
    m = _CERT_NOTAFTER_RE.match((s or "").strip())
    if not m:
        return None
    mon, day, hms, year = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    if mon not in _CERT_MONTH:
        return None
    hh, mm, ss = (int(x) for x in hms.split(":"))
    try:
        return calendar.timegm((year, _CERT_MONTH[mon], day, hh, mm, ss, 0, 0, 0))
    except (ValueError, OverflowError):
        return None


def _decode_cert_not_after(der: bytes) -> int | None:
    """DER 证书 → notAfter epoch。走标准库 X.509 解析（_test_decode_cert 为非公开 API
    但长期稳定；任何异常都如实返回 None，不影响探测结论）。"""
    import base64
    import tempfile
    b64 = base64.b64encode(der).decode("ascii")
    pem = ("-----BEGIN CERTIFICATE-----\n"
           + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
           + "\n-----END CERTIFICATE-----\n")
    path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False,
                                         encoding="ascii") as f:
            f.write(pem)
            path = f.name
        import ssl as _ssl
        info = _ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
        return _parse_not_after(info.get("notAfter", ""))
    except Exception:
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def check_cert(host: str, port: int, timeout: float) -> tuple[int | None, int | None]:
    """TLS 直连取证书余量。返回 (cert_days, cert_not_after_epoch)；拿不到就是 (None, None)。

    证书链不校验（CERT_NONE 下 getpeercert(binary_form=True) 仍返回原始证书）：
    自签/链不完整的站也要能报「证书还剩几天」；链校验是 curl 任务的职责（有真实退出码）。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError, ValueError):
        return None, None
    if not der:
        return None, None
    na = _decode_cert_not_after(der)
    if na is None:
        return None, None
    return int((na - time.time()) // 86400), na


def run_tcp(task: dict, target: str, resolved_ip: str, dns_server: str,
            dns_time_ms, ts: int) -> dict:
    """task: {target(host[:port]), params{port,timeout,tls,cert_min_days}}。

    resolved_ip 非空时连解析出的 IP（agent 先解析后探测，与 curl/mtr 同构），
    TLS/SNI 场景 server_hostname 仍用原 host，保证带 SNI 的虚拟主机能完成握手。
    """
    params = task.get("params") or {}
    host, port = split_host_port(target or task.get("target", ""), params)
    if not port:
        return make_result(ts, "fail", "other", "缺少端口（目标需 host:port 或 params.port）",
                           dns_server, resolved_ip, dns_time_ms)
    timeout = float(params.get("timeout", 5.0))
    connect_host = resolved_ip or host
    addr: tuple[str, int] = (connect_host, port)
    t0 = time.monotonic()
    try:
        sock = socket.create_connection(addr, timeout=timeout)
    except TimeoutError:
        return make_result(ts, "fail", "timeout", f"{connect_host}:{port} 连接超时({timeout}s)",
                           dns_server, resolved_ip, dns_time_ms)
    except ConnectionRefusedError:
        return make_result(ts, "fail", "refused", f"{connect_host}:{port} 连接被拒绝",
                           dns_server, resolved_ip, dns_time_ms)
    except OSError as e:
        return make_result(ts, "fail", "other",
                           f"{connect_host}:{port} 连接失败: {e}",
                           dns_server, resolved_ip, dns_time_ms)
    rtt_ms = round((time.monotonic() - t0) * 1000, 2)
    metrics: dict = {"rtt_ms": rtt_ms, "rtt_avg": rtt_ms, "port": port,
                     "host": host}
    try:
        sock.close()
    except OSError:
        pass
    if params.get("tls"):
        days, na = check_cert(host, port, timeout)
        if days is not None:
            metrics["cert_days"] = days
            metrics["cert_not_after"] = na
        min_days = params.get("cert_min_days")
        if min_days is not None and days is not None and days < int(min_days):
            return make_result(ts, "fail", "cert_expired",
                               f"证书仅剩 {days} 天（阈值 {min_days} 天）",
                               dns_server, resolved_ip, dns_time_ms, metrics)
    return make_result(ts, "ok", "", "", dns_server, resolved_ip, dns_time_ms, metrics)
