"""纯 Python DNS A 记录解析（RFC1035 UDP/TCP + RFC7858 DoT + RFC8484 DoH）。

选择手写而非 dig/nslookup：Windows 无 dig、CentOS 7 nslookup 行为不一，
且 curl --dns-servers 依赖 c-ares 编译不可依赖 —— 先解析后探测需要跨平台可控的解析器。

解析线路（spec）写法：
  - 223.5.5.5                     自动：UDP → TCP → DoH（该 server 有已知 DoH 端点时）
  - doh:https://dns.alidns.com/dns-query?name={host}&type=A   强制 DoH（{host} 占位符可选）
  - https://223.5.5.5/resolve?name={host}&type=1              裸 URL 等同 doh:
  - dot:1.1.1.1[:853] / 1.1.1.1@853                           强制 DoT（RFC7858）
  - udp:8.8.8.8 / tcp:8.8.8.8[:53]                            强制单一传输（排障用）

非法写法策略（parse_spec，已由单测固化）：
  * 已知前缀但参数残缺（dot: / doh: ）、端口非法（dot:1.1.1.1:abc）、
    DoH URL 非法 → 抛 ValueError（resolve() 会包装成 DnsError kind=dns_formerr）。
  * 未知前缀（如 quic:1.1.1.1）→ 不报错，回退 auto 并把整串当 server，
    由 DNS 层如实报错；这样新写法不会让旧任务直接崩。

传输链历史：UDP → TCP → DoH 用于应对两种真实环境：① UDP 响应被截断(TC位)；
② 本机/企业网代理 TUN 劫持 UDP+TCP 53 返回空应答（2026-10-01 实测）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

DOH_ENDPOINTS = {
    "223.5.5.5": "https://223.5.5.5/resolve?name={host}&type=1",
    "223.3.3.3": "https://223.5.5.5/resolve?name={host}&type=1",
    "119.29.29.29": "https://119.29.29.29/dns-query?name={host}&type=A",
    "1.12.12.12": "https://119.29.29.29/dns-query?name={host}&type=A",
    "8.8.8.8": "https://dns.google/resolve?name={host}&type=1",
    "8.8.4.4": "https://dns.google/resolve?name={host}&type=1",
}


# 代理 TUN 劫持 DNS 时返回的 fake-ip 段（RFC2544 基准测试网段）
FAKE_IP_PREFIXES = ("198.18.", "198.19.")

# 线路 kind；auto = UDP→TCP→DoH 三级回退
SPEC_KINDS = ("auto", "doh", "dot", "udp", "tcp")
_DEFAULT_PORT = {"auto": 53, "udp": 53, "tcp": 53, "dot": 853}
_DOH_DEFAULT_PORT = {"http": 80, "https": 443}

# 自签 DoT 测试用：GPM_DOT_INSECURE=1 关闭证书校验（默认开启校验）
DOT_INSECURE_ENV = "GPM_DOT_INSECURE"
_INSECURE_TRUTHY = {"1", "true", "yes", "on"}

_KNOWN_SCHEME_RE = re.compile(r"^(?P<scheme>doh|dot|udp|tcp):(?P<rest>.*)$", re.IGNORECASE)
_URL_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://")
_BRACKET_V6_RE = re.compile(r"^\[(?P<host>[^\]]+)\](?::(?P<port>[0-9]+))?$")


def is_fake_ip(ip: str) -> bool:
    """是否属于 fake-ip 段。TCP 能被代理 TUN 接管，但 ICMP（ping/mtr）打不到。"""
    return bool(ip) and ip.startswith(FAKE_IP_PREFIXES)


class DnsError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind  # dns_timeout | dns_servfail | nx_domain | dns_formerr | dns_refused
        super().__init__(f"{kind}: {detail}")


# ---------------------------------------------------------------- spec 解析

def _parse_port(text: str) -> int:
    t = (text or "").strip()
    if not t.isdigit():
        raise ValueError(f"端口非法: {text!r}")
    p = int(t)
    if not 1 <= p <= 65535:
        raise ValueError(f"端口越界: {p}")
    return p


def _split_host_port(rest: str, default_port: int) -> tuple[str, int]:
    """解析 <host>[:port] / <host>@port / [v6]:port。IPv6 不带括号时必须原样保留。"""
    rest = (rest or "").strip()
    if not rest:
        raise ValueError("缺少服务器地址")
    if "@" in rest:
        server, _, port_s = rest.rpartition("@")
        server = server.strip()
        if not server:
            raise ValueError(f"缺少服务器地址: {rest!r}")
        return server, _parse_port(port_s)
    m = _BRACKET_V6_RE.match(rest)
    if m:
        return m.group("host"), (_parse_port(m.group("port")) if m.group("port") else default_port)
    if rest.count(":") == 1:  # host:port（IPv6 裸串含多个冒号，不在此分支）
        server, _, port_s = rest.partition(":")
        if not server:
            raise ValueError(f"缺少服务器地址: {rest!r}")
        return server, _parse_port(port_s)
    return rest, default_port


def _normalize_doh_url(rest: str) -> str:
    """把 doh: 后面的内容规范成完整 http(s) URL。"""
    rest = (rest or "").strip()
    if not rest:
        raise ValueError("DoH 缺少 URL")
    m = _URL_SCHEME_RE.match(rest)
    if m:
        scheme = m.group("scheme").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"DoH 仅支持 http(s)，收到 {scheme}://")
        url = rest
    elif "/" in rest:
        url = "https://" + rest
    else:  # 裸主机/IP
        url = DOH_ENDPOINTS.get(rest) or f"https://{rest}/dns-query?name={{host}}&type=A"
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # 端口非法时抛 ValueError
    except ValueError as e:
        raise ValueError(f"DoH URL 非法: {url} ({e})")
    if not parts.hostname:
        raise ValueError(f"DoH URL 缺少主机名: {url}")
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"DoH URL 协议非法: {url}")
    return url


def _host_port_from_url(url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"DoH URL 端口非法: {url} ({e})")
    return host, port or _DOH_DEFAULT_PORT.get(parts.scheme or "", 443)


def parse_spec(spec) -> dict:
    """把线路写法解析为 {kind, server, url, port}。

    kind ∈ auto|doh|dot|udp|tcp；非法走法按模块 docstring 的策略抛 ValueError 或回退 auto。
    """
    if isinstance(spec, dict):  # 允许把 parse_spec 的结果直接回传
        kind = str(spec.get("kind") or "auto").lower()
        if kind not in SPEC_KINDS:
            raise ValueError(f"未知 kind: {kind!r}")
        if kind == "doh":
            url = _normalize_doh_url(str(spec.get("url") or ""))
            server, port = _host_port_from_url(url)
            return {"kind": "doh", "server": server, "url": url, "port": port}
        server = str(spec.get("server") or "").strip()
        raw_port = spec.get("port")
        port = _parse_port(str(raw_port)) if raw_port else _DEFAULT_PORT[kind]
        return {"kind": kind, "server": server, "url": None, "port": port}

    raw = "" if spec is None else str(spec).strip()
    if not raw:
        return {"kind": "auto", "server": "", "url": None, "port": _DEFAULT_PORT["auto"]}

    m = _KNOWN_SCHEME_RE.match(raw)
    if m:
        scheme = m.group("scheme").lower()
        rest = m.group("rest").strip()
        if not rest:
            raise ValueError(f"线路 {scheme}: 缺少地址")
        if scheme == "doh":
            url = _normalize_doh_url(rest)
            server, port = _host_port_from_url(url)
            return {"kind": "doh", "server": server, "url": url, "port": port}
        server, port = _split_host_port(rest, _DEFAULT_PORT[scheme])
        return {"kind": scheme, "server": server, "url": None, "port": port}

    if _URL_SCHEME_RE.match(raw):  # 裸 URL → DoH（_normalize_doh_url 会校验协议）
        url = _normalize_doh_url(raw)
        server, port = _host_port_from_url(url)
        return {"kind": "doh", "server": server, "url": url, "port": port}

    if "@" in raw:  # <IP>@<port> → 强制 DoT
        server, port = _split_host_port(raw, _DEFAULT_PORT["dot"])
        return {"kind": "dot", "server": server, "url": None, "port": port}

    # 裸 IP/域名 → 自动；已知 IP 带上对应 DoH 端点作为第三级兜底
    return {"kind": "auto", "server": raw, "url": DOH_ENDPOINTS.get(raw),
            "port": _DEFAULT_PORT["auto"]}


def _spec_key(sp: dict) -> str:
    """缓存键：必须区分线路，避免 auto 与 dot/udp 等结果互相污染。"""
    if sp["kind"] == "doh":
        return f"doh:{sp['url']}"
    if sp["kind"] == "auto":
        return f"auto:{sp['server']}"
    return f"{sp['kind']}:{sp['server']}:{sp['port']}"


def describe(spec) -> str:
    """UI 短标签，如 'DoT 1.1.1.1:853'、'DoH dns.alidns.com'。非法写法不抛异常。"""
    try:
        sp = parse_spec(spec)
    except ValueError:
        return f"未知线路 {spec}"
    kind = sp["kind"]
    if kind == "auto":
        return f"自动 {sp['server']}" if sp["server"] else "系统默认"
    if kind == "doh":
        return f"DoH {sp['server']}"
    if kind == "dot":
        return f"DoT {sp['server']}:{sp['port']}"
    if kind == "udp":
        return f"UDP {sp['server']}:{sp['port']}"
    return f"TCP {sp['server']}:{sp['port']}"


# ---------------------------------------------------------------- 报文编解码

def _encode_name(name: str) -> bytes:
    out = b""
    for part in name.rstrip(".").split("."):
        b = part.encode("idna") if not part.isascii() else part.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _read_name(msg: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    for _ in range(30):  # 防压缩环
        l = msg[off]
        if l == 0:
            off += 1
            break
        if l & 0xC0 == 0xC0:  # 压缩指针
            ptr = ((l & 0x3F) << 8) | msg[off + 1]
            if not jumped:
                end = off + 2
            off, jumped = ptr, True
            continue
        labels.append(msg[off + 1: off + 1 + l].decode("ascii", "replace"))
        off += 1 + l
    return ".".join(labels), (end if jumped else off)


def _parse_response(msg: bytes, qid: int | None) -> list[tuple[int, int, str]]:
    """返回 [(type, ttl, value)]。qid=None 时跳过事务ID校验（DoH/工具路径）。"""
    if len(msg) < 12:
        raise DnsError("dns_formerr", "响应过短")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", msg[:12])
    if qid is not None and rid != qid:
        raise DnsError("dns_formerr", "事务ID不匹配")
    rcode = flags & 0x0F
    if rcode == 3:
        raise DnsError("nx_domain", "NXDOMAIN")
    if rcode == 2:
        raise DnsError("dns_servfail", "SERVFAIL")
    if rcode == 5:
        raise DnsError("dns_refused", "REFUSED")
    if rcode != 0:
        raise DnsError("dns_formerr", f"rcode={rcode}")
    off = 12
    for _ in range(qd):
        _, off = _read_name(msg, off)
        off += 4
    out = []
    for _ in range(an):
        _, off = _read_name(msg, off)
        rtype, _cls, ttl, rdlen = struct.unpack(">HHIH", msg[off:off + 10])
        off += 10
        rdata = msg[off:off + rdlen]
        off += rdlen
        if rtype == 1 and rdlen == 4:
            out.append((1, ttl, socket.inet_ntoa(rdata)))
        elif rtype == 28 and rdlen == 16:
            out.append((28, ttl, socket.inet_ntop(socket.AF_INET6, rdata)))
        elif rtype == 5:
            cname, _ = _read_name(msg, off - rdlen)
            out.append((5, ttl, cname))
    return out


def _build_query(host: str, qid: bytes, qtype: int = 1) -> bytes:
    return (qid + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
            + _encode_name(host) + struct.pack(">HH", qtype, 1))


# ---------------------------------------------------------------- DoT 帧（RFC7858）

def _dot_frame(msg: bytes) -> bytes:
    """2 字节大端长度前缀 + DNS 报文。"""
    return struct.pack(">H", len(msg)) + msg


def _dot_unframe(data: bytes) -> bytes:
    """拆 DoT 帧，返回其中的 DNS 报文（忽略尾部多余字节）。"""
    if len(data) < 2:
        raise DnsError("dns_formerr", "DoT 帧长度前缀缺失")
    need = struct.unpack(">H", data[:2])[0]
    if len(data) - 2 < need:
        raise DnsError("dns_formerr", f"DoT 帧不完整: 声明 {need} 字节，实到 {len(data) - 2}")
    return data[2:2 + need]


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _dot_ssl_context() -> ssl.SSLContext:
    """默认校验证书；GPM_DOT_INSECURE=1 时关闭（自签 DoT 测试用）。"""
    insecure = str(os.environ.get(DOT_INSECURE_ENV, "")).strip().lower() in _INSECURE_TRUTHY
    if insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _dot_exchange(sock, query: bytes, timeout: float = 2.0) -> bytes:
    """在一根已建好的（TLS）连接上完成一次「组帧发送 → 收帧解帧」。"""
    sock.sendall(_dot_frame(query))
    head = _recv_exact(sock, 2)
    if len(head) < 2:
        raise DnsError("dns_formerr", "DoT 响应长度缺失")
    need = struct.unpack(">H", head)[0]
    return _dot_unframe(head + _recv_exact(sock, need))


# ---------------------------------------------------------------- 各传输实现

def _udp_query(host: str, server: str, timeout: float, rrtype: int = 1) -> bytes:
    qid = os.urandom(2)
    q = _build_query(host, qid, rrtype)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        msg, _ = s.recvfrom(4096)
    except TimeoutError:
        raise DnsError("dns_timeout", f"{server} 查询 {host} 超时")
    except OSError as e:
        raise DnsError("dns_timeout", f"{server} 不可达: {e}")
    finally:
        s.close()
    if int.from_bytes(msg[:2], "big") != int.from_bytes(qid, "big"):
        raise DnsError("dns_formerr", "事务ID不匹配")
    return msg


def _tcp_query(host: str, server: str, timeout: float, rrtype: int = 1) -> bytes:
    qid = os.urandom(2)
    q = _build_query(host, qid, rrtype)
    try:
        s = socket.create_connection((server, 53), timeout=timeout)
        s.settimeout(timeout)
        s.sendall(_dot_frame(q))
        head = _recv_exact(s, 2)
        if len(head) < 2:
            raise DnsError("dns_formerr", "TCP 响应长度缺失")
        buf = _dot_unframe(head + _recv_exact(s, struct.unpack(">H", head)[0]))
        s.close()
    except OSError as e:
        raise DnsError("dns_timeout", f"{server} TCP 失败: {e}")
    if int.from_bytes(buf[:2], "big") != int.from_bytes(qid, "big"):
        raise DnsError("dns_formerr", "事务ID不匹配")
    return buf


def _dot_query(host: str, server: str, timeout: float = 2.0, port: int = 853,
               rrtype: int = 1) -> bytes:
    """RFC7858：TLS 连 server:port（默认 853），长度前缀帧 + DNS 报文。"""
    qid = os.urandom(2)
    q = _build_query(host, qid, rrtype)
    try:
        sock = socket.create_connection((server, port), timeout=timeout)
    except TimeoutError as e:
        raise DnsError("dns_timeout", f"DoT {server}:{port} 连接超时: {e}")
    except ConnectionRefusedError as e:
        raise DnsError("dns_servfail", f"DoT {server}:{port} 连接被拒绝: {e}")
    except OSError as e:
        raise DnsError("dns_timeout", f"DoT {server}:{port} 不可达: {e}")
    try:
        sock.settimeout(timeout)
    except OSError as e:
        sock.close()
        raise DnsError("dns_timeout", f"DoT {server}:{port} 设置超时失败: {e}")
    try:  # 握手阶段单独标注：TLS/证书/对端非 TLS 都归为 handshake 失败
        tls = _dot_ssl_context().wrap_socket(sock, server_hostname=server)
    except TimeoutError as e:
        sock.close()
        raise DnsError("dns_timeout", f"DoT {server}:{port} TLS 握手超时: {e}")
    except ssl.SSLError as e:
        sock.close()
        raise DnsError("dns_servfail", f"DoT {server}:{port} TLS 握手失败: {e}")
    except OSError as e:  # 对端不是 TLS（回环/误配端口）在 Windows 上是 ConnectionAbortedError
        sock.close()
        raise DnsError("dns_servfail", f"DoT {server}:{port} TLS 握手失败: {e}")
    try:
        raw = _dot_exchange(tls, q, timeout)
    except TimeoutError as e:
        raise DnsError("dns_timeout", f"DoT {server}:{port} 超时: {e}")
    except ssl.SSLError as e:
        raise DnsError("dns_servfail", f"DoT {server}:{port} TLS 传输失败: {e}")
    except ConnectionResetError as e:
        raise DnsError("dns_servfail", f"DoT {server}:{port} 连接被重置: {e}")
    except DnsError:
        raise
    except OSError as e:
        raise DnsError("dns_servfail", f"DoT {server}:{port} 传输失败: {e}")
    finally:
        try:
            tls.close()
        except OSError:
            pass
    if int.from_bytes(raw[:2], "big") != int.from_bytes(qid, "big"):
        raise DnsError("dns_formerr", "DoT 事务ID不匹配")
    return raw


def build_doh_url(url: str, host: str, rrtype: int = 1) -> str:
    """RFC8484 GET 组 URL：{host} 占位符替换；无占位符则补 name=&type=A。

    rrtype≠1（如 28=AAAA）时改写/追加 type 参数（含占位符 URL 里写死的 type=1/A）。
    """
    host = (host or "").rstrip(".")
    if "{host}" in url:
        out = url.replace("{host}", urllib.parse.quote(host, safe=""))
    elif re.search(r"[?&]name=", url):
        out = url
    else:
        sep = "&" if "?" in url else "?"
        out = f"{url}{sep}name={urllib.parse.quote(host, safe='')}&type=A"
    if rrtype != 1:
        if re.search(r"[?&]type=", out):
            out = re.sub(r"([?&])type=[^&]*", lambda m: m.group(1) + f"type={rrtype}", out)
        else:
            out += f"&type={rrtype}"
    return out


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={
        "accept": "application/dns-json", "User-Agent": "gpm-dns/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            status = getattr(r, "status", None) or getattr(r, "code", None) or 200
    except urllib.error.HTTPError as e:  # URLError 子类，必须先捕获
        kind = "dns_refused" if e.code in (401, 403, 404) else "dns_servfail"
        raise DnsError(kind, f"DoH HTTP {e.code} {url}")
    except TimeoutError as e:
        raise DnsError("dns_timeout", f"DoH 超时 {url}: {e}")
    except urllib.error.URLError as e:
        raise DnsError("dns_timeout", f"DoH 请求失败 {url}: {e}")
    except OSError as e:
        raise DnsError("dns_timeout", f"DoH 请求失败 {url}: {e}")
    if not (200 <= int(status) < 300):
        raise DnsError("dns_servfail", f"DoH HTTP {status} {url}")
    return body


def _doh_query(host: str, server: str, timeout: float) -> bytes:
    """按 server IP 查内置端点返回原始响应体（历史接口，保留兼容）。"""
    ep = DOH_ENDPOINTS.get(server)
    if not ep:
        raise DnsError("dns_refused", f"{server} 无 DoH 端点")
    return _http_get(build_doh_url(ep, host), timeout)


def _doh_request(url: str, host: str, timeout: float, rrtype: int = 1) -> dict:
    """GET DoH JSON 并解析为 dict；非 2xx / 非 JSON 均给出明确 DnsError。"""
    body = _http_get(build_doh_url(url, host, rrtype), timeout)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise DnsError("dns_formerr", f"DoH 响应非 JSON: {e}")
    if not isinstance(data, dict):
        raise DnsError("dns_formerr", "DoH JSON 顶层不是对象")
    return data


def _extract_answers(msg_or_json: bytes | dict) -> list[tuple[int, int, str]]:
    if isinstance(msg_or_json, dict):  # DoH JSON
        out = []
        for a in msg_or_json.get("Answer") or []:
            out.append((a.get("type", 0), int(a.get("TTL", 60)), a.get("data", "")))
        if msg_or_json.get("Status") == 3:
            raise DnsError("nx_domain", "NXDOMAIN (DoH)")
        if msg_or_json.get("Status") not in (0, None):
            raise DnsError("dns_servfail", f"DoH status={msg_or_json.get('Status')}")
        return out
    return _parse_response(msg_or_json, None)


def _collect_a(answers: list[tuple[int, int, str]]) -> tuple[list[str], int]:
    return _collect_ips(answers, 1)


def _collect_ips(answers: list[tuple[int, int, str]], rtype: int) -> tuple[list[str], int]:
    """收集指定 rtype 的记录值（1=A / 28=AAAA），无地址时回看 CNAME 的 TTL。"""
    ips, min_ttl = [], 300
    for t, ttl, val in answers:
        if t == rtype:
            ips.append(val)
            min_ttl = min(min_ttl, ttl)
    if not ips:
        for t, ttl, _val in answers:
            if t == 5:
                min_ttl = min(min_ttl, ttl)
                break
    return ips, min_ttl


class DnsCache:
    """短 TTL 缓存：(host, dns/spec) -> (ips, expires_at, resolve_ms, transport)。"""

    def __init__(self, ttl: int = 60, max_ttl: int = 300, max_items: int = 4096):
        self.ttl, self.max_ttl, self.max_items = ttl, max_ttl, max_items
        self._d: dict[tuple, tuple] = {}

    def get(self, key: str, server: str) -> tuple | None:
        v = self._d.get((key, server))
        if v and v[1] > time.time():
            return v
        if v:
            self._d.pop((key, server), None)
        return None

    def put(self, key: str, server: str, ips: list[str], ttl_s: int, ms: float,
            transport: str = "udp"):
        if len(self._d) >= self.max_items:
            self._d.clear()  # 简单防膨胀
        eff = min(max(ttl_s, self.ttl), self.max_ttl)
        # 第 5 位存原始 DNS TTL（resolve_detail 用；前 4 位为历史结构，测试按位访问）
        self._d[(key, server)] = (ips, time.time() + eff, ms, transport, ttl_s)


# ---------------------------------------------------------------- 解析入口

_RR_NAME = {1: "A", 28: "AAAA"}


def _query_rr(fetch, host: str, server: str, timeout: float, rrtype: int) -> bytes:
    """A 查询保持旧调用形态 (host, server, timeout)（兼容既有打桩/单测）；
    其它 rrtype（AAAA）给传输函数传第 4 个位置参数。"""
    if rrtype == 1:
        return fetch(host, server, timeout)
    return fetch(host, server, timeout, rrtype)


def _wire_transport(host: str, server: str, transport: str, timeout: float,
                    rrtype: int = 1) -> tuple[list[str], int]:
    """UDP/TCP 单传输查询 + 一跳 CNAME 追溯。"""
    fetch = _udp_query if transport == "udp" else _tcp_query
    answers = _extract_answers(_query_rr(fetch, host, server, timeout, rrtype))
    ips, min_ttl = _collect_ips(answers, rrtype)
    if not ips:
        for rtype, _ttl, val in answers:
            if rtype == 5:
                ips, min_ttl = _collect_ips(
                    _extract_answers(_query_rr(fetch, val, server, timeout, rrtype)), rrtype)
                break
    if not ips:
        raise DnsError("dns_servfail",
                       f"{server}({transport}) 对 {host} 无 {_RR_NAME.get(rrtype, str(rrtype))} 记录")
    return ips, min_ttl


def _dot_transport(host: str, server: str, port: int, timeout: float,
                   rrtype: int = 1) -> tuple[list[str], int]:
    if rrtype == 1:  # A：保持旧调用形态（兼容既有打桩/单测）
        query = lambda h: _dot_query(h, server, timeout, port=port)
    else:
        query = lambda h: _dot_query(h, server, timeout, port=port, rrtype=rrtype)
    answers = _extract_answers(query(host))
    ips, min_ttl = _collect_ips(answers, rrtype)
    if not ips:
        for rtype, _ttl, val in answers:
            if rtype == 5:
                ips, min_ttl = _collect_ips(_extract_answers(query(val)), rrtype)
                break
    if not ips:
        raise DnsError("dns_servfail",
                       f"{server}:{port}(dot) 对 {host} 无 {_RR_NAME.get(rrtype, str(rrtype))} 记录")
    return ips, min_ttl


def _resolve_doh(host: str, url: str, timeout: float,
                 rrtype: int = 1) -> tuple[list[str], int]:
    if rrtype == 1:  # A：保持旧调用形态（兼容既有打桩/单测）
        data = _doh_request(url, host, timeout)
    else:
        data = _doh_request(url, host, timeout, rrtype)
    ips, min_ttl = _collect_ips(_extract_answers(data), rrtype)
    if not ips:
        raise DnsError("dns_servfail",
                       f"{url} (doh) 对 {host} 无 {_RR_NAME.get(rrtype, str(rrtype))} 记录")
    return ips, min_ttl


def _resolve_auto(host: str, sp: dict, timeout: float,
                  rrtype: int = 1) -> tuple[list[str], int, str]:
    """默认线路：UDP → TCP → DoH（有端点时）。"""
    errors: list[DnsError] = []
    for transport in ("udp", "tcp"):
        try:
            ips, ttl = _wire_transport(
                host, sp["server"], transport,
                timeout if transport == "udp" else max(timeout, 3.0), rrtype)
            # fake-ip 检测（RFC2544 基准段，代理 TUN 劫持 DNS 的标志）：
            # 该结果无法代表指定 DNS 服务器的真实线路 → 有 DoH 端点时升级到 DoH。
            # 只对 A 记录生效：fake-ip 是 A 应答劫持，AAAA 不返回 198.18/19 段
            if rrtype == 1 and sp["url"] and all(is_fake_ip(ip) for ip in ips):
                errors.append(DnsError("dns_servfail", f"{transport} 返回 fake-ip({ips[0]})"))
                continue
            return ips, ttl, transport
        except DnsError as e:
            errors.append(e)
    if sp["url"]:
        try:
            if rrtype == 1:  # A：保持旧调用形态（兼容既有打桩/单测）
                ips, ttl = _resolve_doh(host, sp["url"], max(timeout, 6.0))
            else:
                ips, ttl = _resolve_doh(host, sp["url"], max(timeout, 6.0), rrtype)
            return ips, ttl, f"doh:{sp['server']}"
        except DnsError as e:
            errors.append(e)
    raise errors[-1] if errors else DnsError("dns_servfail", f"{sp['server']} 解析失败")


def _resolve_forced(host: str, sp: dict, timeout: float,
                    rrtype: int = 1) -> tuple[list[str], int, str]:
    """显式线路：只走指定传输，不回退（排障时行为可预期）。"""
    kind = sp["kind"]
    if kind == "udp":
        ips, ttl = _wire_transport(host, sp["server"], "udp", timeout, rrtype)
        return ips, ttl, "udp"
    if kind == "tcp":
        ips, ttl = _wire_transport(host, sp["server"], "tcp", max(timeout, 3.0), rrtype)
        return ips, ttl, "tcp"
    if kind == "dot":
        if rrtype == 1:  # A：保持旧调用形态（兼容既有打桩/单测）
            ips, ttl = _dot_transport(host, sp["server"], sp["port"], max(timeout, 3.0))
        else:
            ips, ttl = _dot_transport(host, sp["server"], sp["port"], max(timeout, 3.0), rrtype)
        return ips, ttl, "dot"
    if rrtype == 1:  # A：保持旧调用形态（兼容既有打桩/单测）
        ips, ttl = _resolve_doh(host, sp["url"], max(timeout, 3.0))
    else:
        ips, ttl = _resolve_doh(host, sp["url"], max(timeout, 3.0), rrtype)
    return ips, ttl, "doh"


def _resolve_cached(host: str, spec, timeout: float, cache: DnsCache | None,
                    rrtype: int) -> tuple[list[str], float, str, int | None]:
    """缓存查找 + 一次真实解析。返回 (ips, 耗时ms, transport 标签, ttl秒)。

    ttl：真实解析时为 DNS 应答 TTL；命中缓存时为剩余有效秒数（如实标注）。
    """
    h = (host or "").rstrip(".").lower()
    try:
        sp = parse_spec(spec)
    except ValueError as e:
        raise DnsError("dns_formerr", f"非法 DNS 线路 {spec!r}: {e}")
    key = _spec_key(sp) if rrtype == 1 else f"{_spec_key(sp)}#{rrtype}"
    if cache is not None:
        hit = cache.get(h, key)
        if hit:
            ttl_left = max(0, int(hit[1] - time.time()))
            return hit[0], hit[2], hit[3], ttl_left
    t0 = time.monotonic()
    if sp["kind"] == "auto":
        ips, ttl, label = _resolve_auto(h, sp, timeout, rrtype)
    else:
        ips, ttl, label = _resolve_forced(h, sp, timeout, rrtype)
    ms = (time.monotonic() - t0) * 1000
    if cache is not None:
        cache.put(h, key, ips, ttl, ms, label)
    return ips, ms, label, ttl


def resolve(host: str, spec, timeout: float = 2.0,
            cache: DnsCache | None = None, rrtype: int = 1) -> tuple[list[str], float, str]:
    """一等地解析任意线路写法。返回 (ips, 耗时ms, transport 标签)。

    标签：'udp' / 'tcp' / 'dot' / 'doh'（显式线路）；
    auto 模式下走 DoH 兜底时为 'doh:<server>'，便于 UI 如实展示用了哪条线。
    rrtype：1=A（默认）；28=AAAA（ip_version=6 的任务用），缓存键按 rrtype 区分。
    """
    ips, ms, label, _ttl = _resolve_cached(host, spec, timeout, cache, rrtype)
    return ips, ms, label


def resolve_detail(host: str, spec, timeout: float = 2.0,
                   cache: DnsCache | None = None,
                   rrtype: int = 1) -> tuple[list[str], float, str, int | None]:
    """同 resolve，额外返回 DNS TTL（dnsmon 逐线路展示用）。"""
    return _resolve_cached(host, spec, timeout, cache, rrtype)


def resolve_a(host: str, server: str, timeout: float = 2.0,
              cache: DnsCache | None = None) -> tuple[list[str], float, str]:
    """向后兼容入口（agent/prober 在用，签名与返回三元组不变）。

    server 既可以是裸 IP（auto：UDP → TCP → DoH），也可以是 doh:/dot:/udp:/tcp: 线路写法。
    """
    return resolve(host, server, timeout=timeout, cache=cache)


def resolve_aaaa(host: str, server: str, timeout: float = 2.0,
                 cache: DnsCache | None = None) -> tuple[list[str], float, str]:
    """AAAA 记录解析（ip_version=6 的任务）。传输与回退链路同 resolve_a：
    UDP → TCP → DoH（auto 线路时），fake-ip 升级只对 A 生效，此处不参与。"""
    return resolve(host, server, timeout=timeout, cache=cache, rrtype=28)
