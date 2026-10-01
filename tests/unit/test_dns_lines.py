"""DoH/DoT 一等解析线路单测：spec 解析、DoT 组帧/解帧、DoH URL 构造、缓存键、错误路径。

不依赖外网：全部用 monkeypatch 或本机回环（127.0.0.1）替身。
需要监听回环端口的用例用 skipif 守护，环境不允许时明确跳过而不是假装通过。
"""
import json
import socket
import struct
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.common import dnsres as D  # noqa: E402


# ---------------------------------------------------------------- 测试替身

def _wire_a(host: str, ip: str, ttl: int = 120, qid: int = 0) -> bytes:
    """构造一个合法的 DNS 响应：header + question + A（名称用压缩指针）。"""
    qname = D._encode_name(host)
    header = struct.pack(">HHHHHH", qid, 0x8180, 1, 1, 0, 0)
    question = qname + struct.pack(">HH", 1, 1)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, ttl, 4) + socket.inet_aton(ip)
    return header + question + answer


def _doh_json(ip: str, ttl: int = 60) -> dict:
    return {"Status": 0, "Answer": [{"name": "x", "type": 1, "TTL": ttl, "data": ip}]}


class _FakeSock:
    """只实现 recv/sendall 的假 socket，用于免网络验证 _dot_exchange 的收发帧。"""

    def __init__(self, reply: bytes):
        self.reply = reply
        self.sent = b""

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, n: int) -> bytes:
        out, self.reply = self.reply[:n], self.reply[n:]
        return out


class _FakeResp:
    """urllib.request.urlopen 的替身。"""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status = body, status

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _loopback_ok() -> bool:
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        return True
    except OSError:
        return False


NET_OK = _loopback_ok()
needs_loopback = pytest.mark.skipif(not NET_OK, reason="本机无法监听回环端口，跳过本地网络用例")


def _listener() -> tuple[socket.socket, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


# ---------------------------------------------------------------- parse_spec

def test_parse_spec_bare_ip_is_auto():
    sp = D.parse_spec("223.5.5.5")
    assert sp == {"kind": "auto", "server": "223.5.5.5", "url": D.DOH_ENDPOINTS["223.5.5.5"],
                  "port": 53}


def test_parse_spec_bare_ip_without_endpoint():
    sp = D.parse_spec("1.1.1.1")
    assert sp["kind"] == "auto" and sp["server"] == "1.1.1.1" and sp["url"] is None and sp["port"] == 53


def test_parse_spec_bare_domain_is_auto():
    sp = D.parse_spec("dns.alidns.com")
    assert sp["kind"] == "auto" and sp["server"] == "dns.alidns.com" and sp["url"] is None


def test_parse_spec_empty_is_auto_blank():
    assert D.parse_spec("") == {"kind": "auto", "server": "", "url": None, "port": 53}
    assert D.parse_spec(None)["kind"] == "auto"
    assert D.parse_spec("   ")["server"] == ""


def test_parse_spec_doh_full_url_with_placeholder():
    url = "https://223.5.5.5/resolve?name={host}&type=1"
    sp = D.parse_spec("doh:" + url)
    assert sp == {"kind": "doh", "server": "223.5.5.5", "url": url, "port": 443}


def test_parse_spec_doh_bare_url_without_prefix():
    sp = D.parse_spec("https://dns.alidns.com/dns-query")
    assert sp["kind"] == "doh" and sp["server"] == "dns.alidns.com" and sp["port"] == 443


def test_parse_spec_http_url_and_explicit_port():
    sp = D.parse_spec("http://127.0.0.1:8053/dns-query")
    assert sp["kind"] == "doh" and sp["server"] == "127.0.0.1" and sp["port"] == 8053


def test_parse_spec_doh_bare_host_gets_rfc8484_get():
    sp = D.parse_spec("doh:dns.alidns.com")
    assert sp["kind"] == "doh" and sp["url"] == "https://dns.alidns.com/dns-query?name={host}&type=A"
    assert sp["server"] == "dns.alidns.com" and sp["port"] == 443


def test_parse_spec_doh_ip_reuses_builtin_endpoint():
    sp = D.parse_spec("doh:223.5.5.5")
    assert sp["kind"] == "doh" and sp["url"] == D.DOH_ENDPOINTS["223.5.5.5"] and sp["server"] == "223.5.5.5"


def test_parse_spec_dot_forms():
    assert D.parse_spec("dot:1.1.1.1") == {"kind": "dot", "server": "1.1.1.1", "url": None, "port": 853}
    assert D.parse_spec("dot:1.1.1.1:8853")["port"] == 8853
    assert D.parse_spec("DOT:1.1.1.1:8853")["kind"] == "dot"  # 前缀大小写不敏感
    sp = D.parse_spec("1.1.1.1@853")  # 裸 IP@port → 强制 DoT
    assert sp == {"kind": "dot", "server": "1.1.1.1", "url": None, "port": 853}
    assert D.parse_spec("1.1.1.1@53")["kind"] == "dot"


def test_parse_spec_dot_ipv6():
    assert D.parse_spec("dot:2001:db8::1")["server"] == "2001:db8::1"
    assert D.parse_spec("dot:2001:db8::1")["port"] == 853
    sp = D.parse_spec("dot:[2001:db8::1]:8530")
    assert sp["server"] == "2001:db8::1" and sp["port"] == 8530
    assert D.parse_spec("2001:db8::1")["kind"] == "auto"  # 裸 IPv6 不能被当成 scheme


def test_parse_spec_udp_tcp_prefix():
    assert D.parse_spec("udp:8.8.8.8") == {"kind": "udp", "server": "8.8.8.8", "url": None, "port": 53}
    assert D.parse_spec("tcp:8.8.8.8:5353")["port"] == 5353
    assert D.parse_spec("udp:8.8.8.8@5353") == {"kind": "udp", "server": "8.8.8.8", "url": None,
                                                "port": 5353}


def test_parse_spec_doh_dict_roundtrip():
    sp = D.parse_spec("doh:https://dns.alidns.com/dns-query")
    assert D.parse_spec(sp) == sp
    sp2 = D.parse_spec("dot:1.1.1.1:853")
    assert D.parse_spec(sp2) == sp2


@pytest.mark.parametrize("bad", [
    "dot:", "doh:", "udp:", "tcp:",          # 前缀后为空
    "dot:1.1.1.1:abc",                        # 端口非数字
    "dot:1.1.1.1:0", "dot:1.1.1.1:70000",     # 端口越界
    "1.1.1.1@abc", "1.1.1.1@99999",           # 裸 @port 非法
    "doh:ftp://dns.example/x",                # 非 http(s)
    "doh:https:///dns-query",                 # 缺主机名
])
def test_parse_spec_illegal_raises(bad):
    """已定义策略：已知前缀但参数非法 → ValueError。"""
    with pytest.raises(ValueError):
        D.parse_spec(bad)


def test_parse_spec_unknown_prefix_falls_back_to_auto():
    """已定义策略：未知前缀不报错，整串当 server 交给 DNS 层如实报错。"""
    sp = D.parse_spec("quic:1.1.1.1")
    assert sp["kind"] == "auto" and sp["server"] == "quic:1.1.1.1" and sp["url"] is None


# ---------------------------------------------------------------- describe

def test_describe_labels():
    assert D.describe("dot:1.1.1.1") == "DoT 1.1.1.1:853"
    assert D.describe("doh:https://dns.alidns.com/dns-query") == "DoH dns.alidns.com"
    assert D.describe("udp:8.8.8.8") == "UDP 8.8.8.8:53"
    assert D.describe("tcp:8.8.8.8:5353") == "TCP 8.8.8.8:5353"
    assert D.describe("223.5.5.5") == "自动 223.5.5.5"
    assert D.describe("") == "系统默认"


def test_describe_never_raises_on_garbage():
    assert D.describe("doh:") == "未知线路 doh:"
    assert D.describe("dot:1.1.1.1:abc") == "未知线路 dot:1.1.1.1:abc"


# ---------------------------------------------------------------- DoH URL

def test_build_doh_url_placeholder():
    url = "https://223.5.5.5/resolve?name={host}&type=1"
    assert D.build_doh_url(url, "www.baidu.com") == "https://223.5.5.5/resolve?name=www.baidu.com&type=1"


def test_build_doh_url_appends_rfc8484_query():
    assert (D.build_doh_url("https://dns.alidns.com/dns-query", "www.baidu.com")
            == "https://dns.alidns.com/dns-query?name=www.baidu.com&type=A")
    # 已有其它 query 参数时用 & 追加
    assert (D.build_doh_url("https://doh.example/q?cd=0", "a.cn")
            == "https://doh.example/q?cd=0&name=a.cn&type=A")


def test_build_doh_url_keeps_existing_name_query():
    url = "https://doh.example/q?name=example.com&type=A"
    assert D.build_doh_url(url, "other.com") == url


def test_build_doh_url_strips_trailing_dot_and_quotes():
    assert D.build_doh_url("https://x/q", "a.b.") == "https://x/q?name=a.b&type=A"


# ---------------------------------------------------------------- DoT 帧

def test_dot_frame_encoding():
    assert D._dot_frame(b"\xaa\xbb\xcc") == b"\x00\x03\xaa\xbb\xcc"
    assert D._dot_frame(b"") == b"\x00\x00"


def test_dot_unframe_roundtrip():
    msg = _wire_a("www.baidu.com", "153.3.238.28")
    assert D._dot_unframe(D._dot_frame(msg)) == msg
    assert D._dot_unframe(D._dot_frame(msg) + b"trailing") == msg  # 忽略尾部多余字节


@pytest.mark.parametrize("bad", [b"", b"\x01", b"\x00\x05\x01\x02"])
def test_dot_unframe_truncated_raises(bad):
    with pytest.raises(D.DnsError) as ei:
        D._dot_unframe(bad)
    assert ei.value.kind == "dns_formerr"


def test_dot_exchange_sends_frame_and_parses_reply():
    query = D._build_query("www.baidu.com", b"\x12\x34")
    reply_wire = _wire_a("www.baidu.com", "153.3.238.28")
    sock = _FakeSock(D._dot_frame(reply_wire))
    got = D._dot_exchange(sock, query, timeout=1.0)
    assert sock.sent == D._dot_frame(query)
    answers = D._extract_answers(got)
    assert D._collect_a(answers) == (["153.3.238.28"], 120)


def test_dot_exchange_short_length_prefix_raises():
    sock = _FakeSock(b"\x00")
    with pytest.raises(D.DnsError) as ei:
        D._dot_exchange(sock, b"query", timeout=1.0)
    assert ei.value.kind == "dns_formerr"


def test_dot_ssl_context_insecure_env(monkeypatch):
    monkeypatch.delenv(D.DOT_INSECURE_ENV, raising=False)
    ctx = D._dot_ssl_context()
    assert ctx.verify_mode.name == "CERT_REQUIRED" and ctx.check_hostname
    monkeypatch.setenv(D.DOT_INSECURE_ENV, "1")
    ctx2 = D._dot_ssl_context()
    assert str(ctx2.verify_mode.name) == "CERT_NONE" and not ctx2.check_hostname


# ---------------------------------------------------------------- DoH 错误路径

def test_doh_request_non_2xx_and_bad_json(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "busy", None, None)

    monkeypatch.setattr(D.urllib.request, "urlopen", boom)
    with pytest.raises(D.DnsError) as ei:
        D._doh_request("https://doh.example/q", "a.cn", 1.0)
    assert ei.value.kind == "dns_servfail" and "503" in str(ei.value)

    def nf(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nope", None, None)

    monkeypatch.setattr(D.urllib.request, "urlopen", nf)
    with pytest.raises(D.DnsError) as ei2:
        D._doh_request("https://doh.example/q", "a.cn", 1.0)
    assert ei2.value.kind == "dns_refused"

    monkeypatch.setattr(D.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b"<html>not json</html>"))
    with pytest.raises(D.DnsError) as ei3:
        D._doh_request("https://doh.example/q", "a.cn", 1.0)
    assert ei3.value.kind == "dns_formerr"

    monkeypatch.setattr(D.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b"[]"))
    with pytest.raises(D.DnsError) as ei4:
        D._doh_request("https://doh.example/q", "a.cn", 1.0)
    assert ei4.value.kind == "dns_formerr"


def test_doh_request_status_not_raised_by_urlopen(monkeypatch):
    monkeypatch.setattr(D.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(json.dumps(_doh_json("1.2.3.4")).encode(),
                                                            status=500))
    with pytest.raises(D.DnsError) as ei:
        D._doh_request("https://doh.example/q", "a.cn", 1.0)
    assert ei.value.kind == "dns_servfail" and "500" in str(ei.value)


def test_doh_request_timeout(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr(D.urllib.request, "urlopen", boom)
    with pytest.raises(D.DnsError) as ei:
        D._doh_request("https://doh.example/q", "a.cn", 1.0)
    assert ei.value.kind == "dns_timeout"


def test_doh_request_ok_and_url_built(monkeypatch):
    seen = {}

    def ok(req, timeout=None):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return _FakeResp(json.dumps(_doh_json("1.2.3.4", ttl=77)).encode())

    monkeypatch.setattr(D.urllib.request, "urlopen", ok)
    data = D._doh_request("https://doh.example/q", "a.cn", 2.5)
    assert seen["url"] == "https://doh.example/q?name=a.cn&type=A"
    assert D._collect_a(D._extract_answers(data)) == (["1.2.3.4"], 77)


def test_doh_status_nxdomain_and_servfail():
    with pytest.raises(D.DnsError) as ei:
        D._extract_answers({"Status": 3, "Answer": []})
    assert ei.value.kind == "nx_domain"
    with pytest.raises(D.DnsError) as ei2:
        D._extract_answers({"Status": 2, "Answer": []})
    assert ei2.value.kind == "dns_servfail"


# ---------------------------------------------------------------- resolve 线路选择

def test_resolve_auto_udp_label(monkeypatch):
    monkeypatch.setattr(D, "_udp_query", lambda h, s, t: _wire_a(h, "1.2.3.4", ttl=90))
    monkeypatch.setattr(D, "_doh_request",
                        lambda *a, **k: pytest.fail("auto 走通 UDP 后不应触发 DoH"))
    ips, _ms, label = D.resolve("example.com", "1.1.1.1", timeout=1.0)
    assert ips == ["1.2.3.4"] and label == "udp"


def test_resolve_auto_udp_fail_then_tcp(monkeypatch):
    def no_udp(h, s, t):
        raise D.DnsError("dns_timeout", "udp 不通")

    monkeypatch.setattr(D, "_udp_query", no_udp)
    monkeypatch.setattr(D, "_tcp_query", lambda h, s, t: _wire_a(h, "5.6.7.8"))
    ips, _ms, label = D.resolve("example.com", "1.1.1.1", timeout=1.0)
    assert ips == ["5.6.7.8"] and label == "tcp"


def test_resolve_auto_fake_ip_upgraded_to_doh(monkeypatch):
    monkeypatch.setattr(D, "_udp_query", lambda h, s, t: _wire_a(h, "198.18.0.84"))
    monkeypatch.setattr(D, "_tcp_query", lambda h, s, t: _wire_a(h, "198.19.0.1"))
    seen = {}

    def fake_doh(url, host, timeout):
        seen["url"], seen["host"] = url, host
        return _doh_json("103.235.46.96")

    monkeypatch.setattr(D, "_doh_request", fake_doh)
    ips, _ms, label = D.resolve("www.jd.com", "223.5.5.5", timeout=1.0)
    assert ips == ["103.235.46.96"]
    assert label == "doh:223.5.5.5"
    assert seen["url"] == D.DOH_ENDPOINTS["223.5.5.5"] and seen["host"] == "www.jd.com"


def test_resolve_forced_udp_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(D, "_udp_query",
                        lambda h, s, t: (_ for _ in ()).throw(D.DnsError("dns_timeout", "udp 不通")))
    monkeypatch.setattr(D, "_tcp_query", lambda h, s, t: pytest.fail("udp: 不应回退 TCP"))
    monkeypatch.setattr(D, "_doh_request", lambda *a, **k: pytest.fail("udp: 不应回退 DoH"))
    with pytest.raises(D.DnsError) as ei:
        D.resolve("example.com", "udp:8.8.8.8", timeout=1.0)
    assert ei.value.kind == "dns_timeout"


def test_resolve_forced_dot_label_and_port(monkeypatch):
    seen = {}

    def fake_dot_transport(host, server, port, timeout):
        seen.update(host=host, server=server, port=port)
        return ["9.9.9.9"], 42

    monkeypatch.setattr(D, "_dot_transport", fake_dot_transport)
    ips, _ms, label = D.resolve("example.com", "dot:1.1.1.1:8853", timeout=1.0)
    assert ips == ["9.9.9.9"] and label == "dot"
    assert seen == {"host": "example.com", "server": "1.1.1.1", "port": 8853}

    ips2, _ms2, label2 = D.resolve("example.com", "1.1.1.1@853", timeout=1.0)
    assert label2 == "dot" and seen["port"] == 853


def test_resolve_forced_doh_uses_given_url(monkeypatch):
    seen = {}

    def fake_get(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return json.dumps(_doh_json("8.8.4.4")).encode()

    # 打桩到 HTTP 层，让真实的 build_doh_url 参与，验证 {host} 占位符被替换
    monkeypatch.setattr(D, "_http_get", fake_get)
    ips, _ms, label = D.resolve("example.com", "doh:https://223.5.5.5/resolve?name={host}&type=1",
                                timeout=1.0)
    assert ips == ["8.8.4.4"] and label == "doh"
    assert seen["url"] == "https://223.5.5.5/resolve?name=example.com&type=1"


def test_resolve_illegal_spec_is_dns_formerr():
    with pytest.raises(D.DnsError) as ei:
        D.resolve("example.com", "dot:1.1.1.1:abc")
    assert ei.value.kind == "dns_formerr"
    assert "非法" in str(ei.value)


def test_resolve_lowercases_and_strips_host(monkeypatch):
    seen = {}
    monkeypatch.setattr(D, "_udp_query",
                        lambda h, s, t: (seen.update(host=h) or _wire_a(h, "1.2.3.4")))
    D.resolve("WWW.Baidu.COM.", "1.1.1.1", timeout=1.0)
    assert seen["host"] == "www.baidu.com"


def test_resolve_accepts_parsed_spec_dict(monkeypatch):
    monkeypatch.setattr(D, "_dot_transport", lambda h, s, p, t: (["7.7.7.7"], 30))
    sp = D.parse_spec("dot:1.1.1.1")
    ips, _ms, label = D.resolve("example.com", sp, timeout=1.0)
    assert ips == ["7.7.7.7"] and label == "dot"


# ---------------------------------------------------------------- 缓存键

def test_cache_key_distinguishes_specs(monkeypatch):
    calls = []

    def fake_udp(h, s, t):
        calls.append(("udp", s, t))
        return _wire_a(h, "1.1.1.1")

    def fake_dot(h, s, t, port=853):
        calls.append(("dot", s, port))
        return _wire_a(h, "2.2.2.2")

    monkeypatch.setattr(D, "_udp_query", fake_udp)
    monkeypatch.setattr(D, "_dot_query", fake_dot)
    cache = D.DnsCache(60, 300)

    ips, ms, label = D.resolve("example.com", "1.1.1.1", timeout=1.0, cache=cache)
    assert (ips, label) == (["1.1.1.1"], "udp") and isinstance(ms, float)
    # 同 spec 第二次命中缓存：不再发起网络
    ips2, ms2, label2 = D.resolve("example.com", "1.1.1.1", timeout=1.0, cache=cache)
    assert (ips2, label2) == (["1.1.1.1"], "udp") and ms2 == ms
    assert len(calls) == 1

    # 同 server 不同线路（auto vs dot）不能共用缓存
    ips3, _ms3, label3 = D.resolve("example.com", "dot:1.1.1.1", timeout=1.0, cache=cache)
    assert (ips3, label3) == (["2.2.2.2"], "dot") and calls[-1] == ("dot", "1.1.1.1", 853)
    assert ("example.com", "auto:1.1.1.1") in cache._d
    assert ("example.com", "dot:1.1.1.1:853") in cache._d

    # 同 DoT 不同端口也是不同键
    D.resolve("example.com", "dot:1.1.1.1:8853", timeout=1.0, cache=cache)
    assert ("example.com", "dot:1.1.1.1:8853") in cache._d
    assert len(calls) == 3


def test_cache_distinguishes_hosts(monkeypatch):
    calls = []
    monkeypatch.setattr(D, "_udp_query",
                        lambda h, s, t: (calls.append(h) or _wire_a(h, "1.1.1.1")))
    cache = D.DnsCache(60, 300)
    D.resolve("a.com", "1.1.1.1", cache=cache)
    D.resolve("b.com", "1.1.1.1", cache=cache)
    assert calls == ["a.com", "b.com"]


def test_cache_expiry_uses_ttl(monkeypatch):
    monkeypatch.setattr(D, "_udp_query", lambda h, s, t: _wire_a(h, "1.1.1.1", ttl=5))
    cache = D.DnsCache(ttl=1, max_ttl=10)
    D.resolve("a.com", "1.1.1.1", cache=cache)
    entry = cache._d[("a.com", "auto:1.1.1.1")]
    # 记录 TTL=5（下限 cache.ttl=1、上限 max_ttl=10）
    assert 4 < entry[1] - time.time() <= 5.5

    monkeypatch.setattr(D, "_udp_query", lambda h, s, t: _wire_a(h, "1.1.1.1", ttl=300))
    cache2 = D.DnsCache(ttl=1, max_ttl=10)
    D.resolve("a.com", "1.1.1.1", cache=cache2)
    assert 9 < cache2._d[("a.com", "auto:1.1.1.1")][1] - time.time() <= 10.5  # 被 max_ttl 截断


# ---------------------------------------------------------------- 真实 socket 路径（仅回环）

def test_dot_connection_refused_is_clear_error(monkeypatch):
    """连接被拒 → 明确的 dns_servfail（本机有代理 TUN 时回环拒连表现为超时，
    因此此处打桩 create_connection，保证跨环境行为可断言）。"""
    def refused(addr, timeout=None):
        raise ConnectionRefusedError(10061, "Connection refused")

    monkeypatch.setattr(D.socket, "create_connection", refused)
    with pytest.raises(D.DnsError) as ei:
        D._dot_query("example.com", "127.0.0.1", timeout=1.0, port=853)
    assert ei.value.kind == "dns_servfail"
    assert "拒绝" in str(ei.value)


@needs_loopback
def test_dot_closed_port_yields_clear_error():
    """真实 socket：关闭的回环端口也必须给出 DnsError（代理 TUN 下可能是 timeout）。"""
    s, port = _listener()
    s.close()
    with pytest.raises(D.DnsError) as ei:
        D._dot_query("example.com", "127.0.0.1", timeout=1.0, port=port)
    assert ei.value.kind in ("dns_servfail", "dns_timeout")


@needs_loopback
def test_dot_plaintext_server_fails_tls_handshake():
    """回环上放一个非 TLS 服务：握手必须报清晰的 dns_servfail。"""
    lsock, port = _listener()

    def serve():
        try:
            conn, _ = lsock.accept()
            try:
                conn.sendall(b"NOT-A-TLS-SERVER\r\n")
            finally:
                conn.close()
        except OSError:
            pass
        finally:
            lsock.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(D.DnsError) as ei:
        D._dot_query("example.com", "127.0.0.1", timeout=2.0, port=port)
    assert ei.value.kind == "dns_servfail"
    assert "TLS" in str(ei.value)


@needs_loopback
def test_dot_silent_server_times_out():
    """连上但不回任何字节：必须在 timeout 内报 dns_timeout。"""
    lsock, port = _listener()

    def serve():
        try:
            conn, _ = lsock.accept()
            time.sleep(2.0)  # 握手期静默
            conn.close()
        except OSError:
            pass
        finally:
            lsock.close()

    threading.Thread(target=serve, daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(D.DnsError) as ei:
        D._dot_query("example.com", "127.0.0.1", timeout=0.6, port=port)
    assert ei.value.kind == "dns_timeout"
    assert time.monotonic() - t0 < 1.8


def test_dot_insecure_env_is_read_at_call_time(monkeypatch):
    """GPM_DOT_INSECURE 在调用时读取，测试可随时开关（模块导入时不固化）。"""
    monkeypatch.setenv(D.DOT_INSECURE_ENV, "1")
    assert not D._dot_ssl_context().check_hostname
    monkeypatch.setenv(D.DOT_INSECURE_ENV, "0")
    assert D._dot_ssl_context().check_hostname


# ---------------------------------------------------------------- 兼容性

def test_resolve_a_signature_unchanged():
    import inspect
    sig = inspect.signature(D.resolve_a)
    assert list(sig.parameters) == ["host", "server", "timeout", "cache"]
    assert sig.parameters["timeout"].default == 2.0
    assert sig.parameters["cache"].default is None
    assert list(inspect.signature(D.DnsCache.get).parameters) == ["self", "key", "server"]
    assert list(inspect.signature(D.DnsCache.put).parameters) == ["self", "key", "server", "ips",
                                                                  "ttl_s", "ms", "transport"]
    assert D.FAKE_IP_PREFIXES == ("198.18.", "198.19.") and D.is_fake_ip("198.18.0.1")


def test_resolve_a_accepts_spec_and_bare_ip(monkeypatch):
    monkeypatch.setattr(D, "_udp_query", lambda h, s, t: _wire_a(h, "1.1.1.1"))
    monkeypatch.setattr(D, "_dot_transport", lambda h, s, p, t: (["2.2.2.2"], 20))
    assert D.resolve_a("a.com", "8.8.4.4")[0] == ["1.1.1.1"]
    assert D.resolve_a("a.com", "dot:8.8.4.4")[0] == ["2.2.2.2"]
