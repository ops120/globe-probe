"""P2 探测扩展单测：TCP 端口 / DNS 监控 / curl 增强 / IPv6 / mtr 增强。

真实网络用例（本机可上网）对活服务 127.0.0.1:8620、example.com、https://www.baidu.com 实测；
活服务/外网不可达时如实 skipif，而不是假装通过。fixture 样本驱动解析逻辑。
"""
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.common import dnsres as D  # noqa: E402
from gpm.common.dnsres import is_fake_ip  # noqa: E402
from gpm.probers.base import ping_cmd  # noqa: E402
from gpm.probers.curl import curl_cmd, fetch_cert_days, run_curl  # noqa: E402
from gpm.probers.dnsmon import answers_hit, run_dnsmon  # noqa: E402
from gpm.probers.mtr import _parse_json, _parse_text_report  # noqa: E402
from gpm.probers.tcp import _parse_not_after, run_tcp, split_host_port  # noqa: E402

TS = 100


def _port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def _live_server(port: int = 8620) -> bool:
    """8620 上是**真正在服务的** gpm（/api/health 回 ok），而不是端口开着的半启动态。

    只探端口不够：服务端重启窗口里端口已经 bind 但应用还没就绪，探测会误判成
    「有服务」然后让用例失败（复核中撞到过一次）。这里看 health 是否真的回 JSON。
    """
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=2) as r:
            # 注意：先去掉空格再比对，否则 '"ok": true' 永远匹配不上（我写错过一次）
            return '"ok":true' in r.read().decode("utf-8", "replace").replace(" ", "")
    except Exception:  # noqa: BLE001
        return False


def _free_listener() -> tuple[socket.socket, int]:
    """起一个真实监听器（保证端口活），测试结束自动关闭。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def _net_ok() -> bool:
    try:
        socket.create_connection(("223.5.5.5", 53), timeout=2).close()
        return True
    except OSError:
        return False


needs_net = pytest.mark.skipif(not _net_ok(), reason="本机无法访问外网，跳过真实用例")

# ---------------------------------------------------------------- tcp 探测


def test_tcp_split_host_port():
    assert split_host_port("127.0.0.1:8620", {}) == ("127.0.0.1", 8620)
    assert split_host_port("example.com:443", {}) == ("example.com", 443)
    assert split_host_port("[2001:db8::1]:443", {}) == ("2001:db8::1", 443)
    assert split_host_port("[2001:db8::1]", {}) == ("2001:db8::1", None)
    assert split_host_port("example.com", {"port": 8443}) == ("example.com", 8443)
    assert split_host_port("2001:db8::1", {"port": "853"}) == ("2001:db8::1", 853)
    assert split_host_port("example.com:bad", {}) == ("example.com", None)
    assert split_host_port("", {}) == ("", None)


def test_tcp_connect_ok_and_rtt():
    """真实连本机一次性监听器：ok + rtt_ms>0（不依赖线上实例）。"""
    lsock, port = _free_listener()
    try:
        r = run_tcp({"target": f"127.0.0.1:{port}", "params": {"timeout": 3}},
                    f"127.0.0.1:{port}", "127.0.0.1", "", None, TS)
        assert r["status"] == "ok", r
        assert r["metrics"]["rtt_ms"] > 0
        assert r["metrics"]["rtt_avg"] == r["metrics"]["rtt_ms"]  # 复用 rtt 聚合约定
        assert r["metrics"]["port"] == port
    finally:
        lsock.close()


@pytest.mark.skipif(not _live_server(8620), reason="本机 8620 无活着的 gpm，跳过")
def test_tcp_connect_live_server_8620():
    """真实连线上活服务 127.0.0.1:8620：ok + rtt_ms>0。"""
    r = run_tcp({"target": "127.0.0.1:8620", "params": {"timeout": 3}},
                "127.0.0.1:8620", "", "", None, TS)
    assert r["status"] == "ok"
    assert r["metrics"]["rtt_ms"] > 0


def test_tcp_refused_and_timeout_classification(monkeypatch):
    """错误分类：refused / timeout（打桩 socket，环境无关地固化分类逻辑）。"""
    from gpm.probers import tcp as T

    def refused(addr, timeout=None):
        raise ConnectionRefusedError(10061, "Connection refused")

    monkeypatch.setattr(T.socket, "create_connection", refused)
    r = T.run_tcp({"target": "127.0.0.1:9", "params": {}}, "127.0.0.1:9", "127.0.0.1", "", None, TS)
    assert r["status"] == "fail" and r["error_class"] == "refused"

    def timedout(addr, timeout=None):
        raise socket.timeout("timed out")

    monkeypatch.setattr(T.socket, "create_connection", timedout)
    r2 = T.run_tcp({"target": "127.0.0.1:9", "params": {}}, "127.0.0.1:9", "127.0.0.1", "", None, TS)
    assert r2["status"] == "fail" and r2["error_class"] == "timeout"


def test_tcp_real_closed_port():
    """真实关闭端口（先取再放）：本机代理 TUN 下可能表现为 timeout，两者都是如实分类。"""
    s, port = _free_listener()
    s.close()
    r = run_tcp({"target": f"127.0.0.1:{port}", "params": {"timeout": 2}},
                f"127.0.0.1:{port}", "127.0.0.1", "", None, TS)
    assert r["status"] == "fail"
    assert r["error_class"] in ("refused", "timeout")


def test_tcp_missing_port_is_fail():
    r = run_tcp({"target": "example.com", "params": {}}, "example.com", "", "", None, TS)
    assert r["status"] == "fail" and "缺少端口" in r["error"]


def test_tcp_cert_not_after_fixture():
    import calendar
    na = _parse_not_after("Nov 28 12:00:00 2026 GMT")
    assert na == calendar.timegm((2026, 11, 28, 12, 0, 0, 0, 0, 0))
    assert _parse_not_after("Feb 29 00:00:00 2028 GMT") is not None
    assert _parse_not_after("garbage") is None
    assert _parse_not_after("") is None


@needs_net
def test_tcp_tls_cert_real():
    """真实 https 站点：tls=true 拿到 cert_days/cert_not_after。"""
    r = run_tcp({"target": "www.baidu.com:443", "params": {"tls": True, "timeout": 8}},
                "www.baidu.com:443", "", "", None, TS)
    assert r["status"] == "ok"
    assert r["metrics"].get("cert_days", -1) > 0
    assert r["metrics"].get("cert_not_after", 0) > time.time()

# ---------------------------------------------------------------- dns 监控


def test_dnsmon_lines_change_and_consistency(monkeypatch):
    """多线路聚合 / 一致性 / rtt 均值 / 值变更（打桩解析，fixture 化）。"""
    from gpm.probers import dnsmon as DMon
    answers = {"1.1.1.1": ["1.0.0.1", "1.1.1.1"], "8.8.8.8": ["1.0.0.1", "1.1.1.1"]}

    def fake_detail(host, spec, timeout=2.0, cache=None, rrtype=1):
        if spec == "":
            return ["1.0.0.1", "1.1.1.1"], 5.0, "system", 60
        return answers[spec], 12.0, "udp", 300

    monkeypatch.setattr(DMon, "resolve_detail", fake_detail)
    monkeypatch.setattr(DMon, "_system_resolve", lambda h, t: (["1.0.0.1"], 3.0))
    task = {"target": "example.com", "dns": ["1.1.1.1", "8.8.8.8"], "params": {}}
    r = run_dnsmon(task, None, TS)
    assert r["status"] == "ok"
    assert r["metrics"]["consistent"] is True
    assert r["metrics"]["rtt_ms"] == 12.0 and r["metrics"]["rtt_avg"] == 12.0
    assert r["metrics"]["lines"]["1.1.1.1"]["ttl"] == 300
    # 未传 prev → changed=False
    assert r["metrics"]["changed"] is False
    # 变更：上次答案集与本次不同
    r2 = run_dnsmon(task, None, TS, prev=["9.9.9.9"])
    assert r2["metrics"]["changed"] is True
    assert r2["metrics"]["prev_answers"] == ["9.9.9.9"]
    # 未变更：上次与本次一致
    r3 = run_dnsmon(task, None, TS, prev=["1.0.0.1", "1.1.1.1"])
    assert r3["metrics"]["changed"] is False
    assert "prev_answers" not in r3["metrics"]


def test_dnsmon_inconsistent_lines(monkeypatch):
    from gpm.probers import dnsmon as DMon

    def fake_detail(host, spec, timeout=2.0, cache=None, rrtype=1):
        return {"1.1.1.1": ["1.1.1.1"], "8.8.8.8": ["8.8.8.8"]}[spec], 10.0, "udp", 120

    monkeypatch.setattr(DMon, "resolve_detail", fake_detail)
    r = run_dnsmon({"target": "example.com", "dns": ["1.1.1.1", "8.8.8.8"], "params": {}}, None, TS)
    assert r["status"] == "ok" and r["metrics"]["consistent"] is False


def test_dnsmon_all_lines_fail(monkeypatch):
    from gpm.probers import dnsmon as DMon

    def boom(host, spec, timeout=2.0, cache=None, rrtype=1):
        raise DMon.DnsError("dns_timeout", f"{spec} 超时")

    monkeypatch.setattr(DMon, "resolve_detail", boom)
    r = run_dnsmon({"target": "example.com", "dns": ["udp:8.8.8.8", "tcp:8.8.4.4"], "params": {}}, None, TS)
    assert r["status"] == "fail" and r["error_class"] == "dns_error"
    assert all(not v["ok"] for v in r["metrics"]["lines"].values())


def test_dnsmon_fake_ip_upgrade(monkeypatch):
    """fake-ip 场景（mock）：线路应答全为 198.18/19 段 → DoH 兜底升级并如实标注。"""
    from gpm.probers import dnsmon as DMon
    calls: list = []

    def fake_detail(host, spec, timeout=2.0, cache=None, rrtype=1):
        calls.append(spec)
        if str(spec).startswith("doh:"):
            return ["93.184.216.34"], 30.0, "doh", 300
        return ["198.18.0.84"], 1.0, "udp", 1

    monkeypatch.setattr(DMon, "resolve_detail", fake_detail)
    r = run_dnsmon({"target": "example.com", "dns": ["udp:223.5.5.5"], "params": {}}, None, TS)
    assert r["status"] == "ok"
    line = r["metrics"]["lines"]["udp:223.5.5.5"]
    assert line["ok"] is True and line["answers"] == ["93.184.216.34"]
    assert line["fake_ip_upgraded"] is True
    assert any(str(s).startswith("doh:") for s in calls), "应尝试 DoH 兜底"


def test_dnsmon_expected_hit_and_miss():
    assert answers_hit(["1.2.3.4"], ["1.2.3.4"], "") is True
    assert answers_hit(["1.2.3.4"], ["10.0.0.0/8"], "") is False
    assert answers_hit(["10.1.2.3"], ["10.0.0.0/8"], "") is True
    assert answers_hit(["93.184.216.34"], [], r"^\d+\.\d+\.") is True
    assert answers_hit(["93.184.216.34"], [], r"^bad") is False
    assert answers_hit(["1.2.3.4"], ["1.2.3.4"], "zzz") is True  # 任一命中即可


@needs_net
def test_dnsmon_real_example_com():
    """真实解析 example.com（udp:223.5.5.5，fake-ip 自动 DoH 升级）拿到真实 IP。"""
    r = run_dnsmon({"target": "example.com", "dns": ["udp:223.5.5.5"], "params": {}},
                   None, TS, 6.0)
    assert r["status"] == "ok"
    line = r["metrics"]["lines"]["udp:223.5.5.5"]
    assert line["ok"] and line["answers"]
    assert not any(is_fake_ip(a) for a in line["answers"]), "必须拿到非 fake-ip 的真实答案"
    assert line["ms"] >= 0 and r["metrics"]["rtt_ms"] is not None


@needs_net
def test_dnsmon_expected_mismatch_real():
    r = run_dnsmon({"target": "example.com", "dns": ["udp:223.5.5.5"],
                    "params": {"expected_ips": ["10.0.0.0/8"]}}, None, TS, 6.0)
    assert r["status"] == "fail" and r["error_class"] == "expect_mismatch"

# ---------------------------------------------------------------- curl 增强


def test_curl_cmd_enhancement_flags():
    args = curl_cmd("http://a.com/x", 5, follow=False, method="POST",
                    headers=["X-A: 1", "X-B: 2"], body="hello")
    assert "-L" not in args, "follow=False 不应带 -L"
    assert args[args.index("-X") + 1] == "POST"
    assert args[args.index("-H") + 1] == "X-A: 1"
    assert args[args.index("--data-binary") + 1] == "hello"
    head = curl_cmd("http://a.com/x", 5, method="HEAD")
    assert "-I" in head and "-X" not in head
    plain = curl_cmd("http://a.com/x", 5)
    assert "-L" in plain and "-X" not in plain   # 缺省维持原行为（GET + 跟随）


@pytest.mark.skipif(not _live_server(8620), reason="本机 8620 无活着的 gpm，跳过")
def test_curl_keyword_hit_and_miss_live():
    """对活服务 /api/health（响应含 "ok"）实测 keyword 命中与未命中。"""
    url = "http://127.0.0.1:8620/api/health"
    ok = run_curl({"target": "127.0.0.1", "params": {"timeout": 5, "keyword": "ok",
                                                    "method": "GET", "headers": {"X-Probe": "gpm"}}},
                  url, "", "", None, TS)
    assert ok["status"] == "ok" and ok["metrics"]["keyword_hit"] is True
    miss = run_curl({"target": "127.0.0.1", "params": {"timeout": 5,
                                                       "keyword": "notexist-keyword-xyz"}},
                    url, "", "", None, TS)
    assert miss["status"] == "fail" and miss["error_class"] == "keyword_miss"
    assert miss["metrics"]["keyword_hit"] is False
    # 正则命中
    rx = run_curl({"target": "127.0.0.1", "params": {"timeout": 5, "regex": r'"ok"\s*:\s*true'}},
                  url, "", "", None, TS)
    assert rx["status"] == "ok" and rx["metrics"]["regex_hit"] is True


@needs_net
def test_curl_cert_check_real_baidu():
    """cert_check 对 https://www.baidu.com 实测 cert_days>0。"""
    r = run_curl({"target": "www.baidu.com", "params": {"timeout": 10, "cert_check": True}},
                 "https://www.baidu.com", "", "", None, TS)
    assert r["status"] == "ok"
    assert r["metrics"].get("cert_days", -1) > 0


@needs_net
def test_fetch_cert_days_direct():
    days, na = fetch_cert_days("https://www.baidu.com", timeout=8)
    assert days is not None and days > 0 and na is not None and na > time.time()

# ---------------------------------------------------------------- IPv6


def test_ping_cmd_ip_version_flags(monkeypatch):
    """双平台 flag 拼接：显式 4/6 传 -4/-6，auto 不传。"""
    import gpm.probers.base as B
    for platform in (True, False):
        monkeypatch.setattr(B, "IS_WINDOWS", platform)
        c6 = ping_cmd("2001:db8::1", 4, 2.0, "6")
        c4 = ping_cmd("1.2.3.4", 4, 2.0, "4")
        ca = ping_cmd("1.2.3.4", 4, 2.0, "auto")
        assert "-6" in c6 and "-4" not in c6
        assert "-4" in c4 and "-6" not in c4
        assert "-4" not in ca and "-6" not in ca


PING_OK = """
Pinging example.com [2001:db8::1] with 32 bytes of data:
Reply from 2001:db8::1: bytes=32 time=25ms TTL=57
Reply from 2001:db8::1: bytes=32 time=24ms TTL=57

Ping statistics for 2001:db8::1:
    Packets: Sent = 2, Received = 2, Lost = 0 (0% loss),
"""


def test_run_ping_ip_version6_uses_aaaa(monkeypatch):
    """ip_version=6 → 走 AAAA 解析并给 ping 传 -6。"""
    from gpm.probers import ping as P
    called = {}

    def fake_aaaa(host, server, timeout=2.0, cache=None):
        called["aaaa"] = (host, server)
        return ["2001:db8::1"], 5.0, "udp"

    monkeypatch.setattr(P, "resolve_aaaa", fake_aaaa)
    seen_cmd = {}

    def fake_run(args, timeout):
        seen_cmd["args"] = args
        return 0, PING_OK, ""

    monkeypatch.setattr(P, "run_cmd", fake_run)
    r = P.run_ping({"target": "example.com", "params": {"ip_version": "6"}},
                   "223.5.5.5", None, TS)
    assert called["aaaa"] == ("example.com", "223.5.5.5")
    assert r["status"] == "ok" and r["resolved_ip"] == "2001:db8::1"
    assert "-6" in seen_cmd["args"]


def _wire_aaaa(host: str, v6: str, ttl: int = 120, qid: int = 0) -> bytes:
    qname = D._encode_name(host)
    header = struct.pack(">HHHHHH", qid, 0x8180, 1, 1, 0, 0)
    question = qname + struct.pack(">HH", 28, 1)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 28, 1, ttl, 16) + socket.inet_pton(socket.AF_INET6, v6)
    return header + question + answer


def test_resolve_aaaa_wire_fixture(monkeypatch):
    """AAAA 字节流解析：rtype 28 + inet_ntop；缓存键按 rrtype 区分。"""
    seen = {}

    def fake_udp(h, s, t, rrtype=1):
        seen["rrtype"] = rrtype
        return _wire_aaaa(h, "240e:56:4000:800a::1e")

    monkeypatch.setattr(D, "_udp_query", fake_udp)
    monkeypatch.setattr(D, "_doh_request",
                        lambda *a, **k: pytest.fail("UDP 成功时不应触发 DoH"))
    ips, ms, label = D.resolve_aaaa("v6.example.com", "1.1.1.1", timeout=1.0)
    assert ips == ["240e:56:4000:800a::1e"] and label == "udp"
    assert seen["rrtype"] == 28
    # 缓存键：AAAA 与 A 不共用
    cache = D.DnsCache(60, 300)
    D.resolve_aaaa("v6.example.com", "1.1.1.1", timeout=1.0, cache=cache)
    assert ("v6.example.com", "auto:1.1.1.1#28") in cache._d


def test_resolve_aaaa_fake_ip_check_not_applied(monkeypatch):
    """fake-ip 升级只对 A 生效：AAAA 路径即使返回 198.18 段也不触发 DoH 升级。"""
    monkeypatch.setattr(
        D, "_wire_transport",
        lambda h, s, tr, t, rrtype=1: (["198.18.0.1"], 1) if rrtype == 28 else (_ for _ in ()).throw(
            AssertionError("AAAA 路径不应走 A 查询")))
    monkeypatch.setattr(D, "_doh_request",
                        lambda *a, **k: pytest.fail("AAAA 不应触发 fake-ip 升级 DoH"))
    ips, _ms, label = D.resolve("v6.example.com", "1.1.1.1", timeout=1.0, rrtype=28)
    assert ips == ["198.18.0.1"] and label == "udp"


def _wire_a(host: str, ip: str, ttl: int = 120, qid: int = 0) -> bytes:
    qname = D._encode_name(host)
    header = struct.pack(">HHHHHH", qid, 0x8180, 1, 1, 0, 0)
    question = qname + struct.pack(">HH", 1, 1)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, ttl, 4) + socket.inet_aton(ip)
    return header + question + answer


def test_resolve_a_still_uses_old_call_shape(monkeypatch):
    """回归：A 查询保持 (host, server, timeout) 旧调用形态（既有打桩兼容）。"""
    monkeypatch.setattr(D, "_udp_query", lambda h, s, t: _wire_a(h, "1.2.3.4"))
    ips, _ms, label = D.resolve("a.com", "1.1.1.1", timeout=1.0)
    assert ips == ["1.2.3.4"] and label == "udp"

# ---------------------------------------------------------------- mtr 增强


MTR_TEXT_ASN = """
Start: 2026-10-02T03:30:00+0800
HOST: bj-ct-01                            Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.31.1                       0.0%    10    2.1   2.0   1.8   2.6   0.2
  2.|-- AS4837 202.97.66.93                0.0%    10    9.2   9.8   8.9  12.1   0.9
  3.|-- AS15169 8.8.8.8                    0.0%    10   74.2  75.1  72.8  79.9   1.9
"""

MTR_JSON_ASN = """{
 "report": {
  "mtr": {"src": "bj-ct-01", "dst": "8.8.8.8", "tests": 10},
  "hubs": [
   {"count": 1, "host": "192.168.31.1", "Loss%": 0.0, "Snt": 10, "Last": 2.1, "Avg": 2.0, "Best": 1.8, "Wrst": 2.6, "StDev": 0.2, "ASN": [4837]},
   {"count": 2, "host": "8.8.8.8", "Loss%": 0.0, "Snt": 10, "Last": 74.2, "Avg": 75.1, "Best": 72.8, "Wrst": 79.9, "StDev": 1.9, "ASN": []}
  ]
 }
}
"""


MTR_TEXT_NOASN = """
Start: 2026-10-02T03:30:00+0800
HOST: bj-ct-01                            Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.31.1                       0.0%    10    2.1   2.0   1.8   2.6   0.2
  2.|-- 202.97.66.93                       0.0%    10    9.2   9.8   8.9  12.1   0.9
  3.|-- 8.8.8.8                            0.0%    10   74.2  75.1  72.8  79.9   1.9
"""


def test_mtr_text_asn_fixture():
    hops = _parse_text_report(MTR_TEXT_ASN, True)
    assert len(hops) == 3
    assert hops[0]["asn"] is None and hops[0]["host"] == "192.168.31.1"
    assert hops[1]["asn"] == 4837 and hops[1]["host"] == "202.97.66.93"
    assert hops[2]["asn"] == 15169
    # 无 -z 的普通文本（真实输出无 AS 前缀）：asn 恒为 None
    plain = _parse_text_report(MTR_TEXT_NOASN, False)
    assert len(plain) == 3
    assert plain[1]["host"] == "202.97.66.93" and plain[1]["asn"] is None


def test_mtr_json_asn_fixture():
    hops = _parse_json(MTR_JSON_ASN, True)
    assert hops[0]["asn"] == 4837
    assert hops[1]["asn"] is None   # 空 ASN 数组 → None


def test_mtr_probe_mode_asn_ipver_flags(monkeypatch):
    """-T/-u/-z/-6 flag 拼接（fixture + 打桩断言）。"""
    from gpm.probers import mtr as M
    monkeypatch.setattr(M, "IS_WINDOWS", False)
    monkeypatch.setattr(M, "_mtr_version", lambda: (0, 95))
    seen = {}

    def fake_run(args, timeout):
        seen["args"] = args
        return 0, MTR_JSON_ASN, ""

    monkeypatch.setattr(M, "run_cmd", fake_run)
    r = M.run_mtr({"target": "8.8.8.8",
                   "params": {"probe_mode": "tcp", "show_asn": True, "ip_version": "6",
                              "cycles": 10}},
                  "", "8.8.8.8", None, TS)
    a = seen["args"]
    assert a[0] == "mtr" and "--json" in a
    assert "-T" in a and "-z" in a and "-6" in a
    assert r["status"] == "ok"
    assert r["metrics"]["probe_mode"] == "tcp" and r["metrics"]["show_asn"] is True
    # udp 模式 → -u
    M.run_mtr({"target": "8.8.8.8", "params": {"probe_mode": "udp"}}, "", "8.8.8.8", None, TS)
    assert "-u" in seen["args"]
    # icmp 默认无 flag
    M.run_mtr({"target": "8.8.8.8", "params": {}}, "", "8.8.8.8", None, TS)
    assert "-T" not in seen["args"] and "-u" not in seen["args"] and "-z" not in seen["args"]


def test_tracert_unsupported_for_tcp_udp(monkeypatch):
    """Windows tracert 无 TCP/UDP 探测能力 → 如实 skipped/unsupported。"""
    from gpm.probers import mtr as M
    monkeypatch.setattr(M, "IS_WINDOWS", True)
    r = M.run_mtr({"target": "8.8.8.8", "params": {"probe_mode": "tcp"}}, "", "8.8.8.8", None, TS)
    assert r["status"] == "skipped" and r["error_class"] == "unsupported"
    assert "tracert" in r["error"] and "tcp" in r["error"]


def test_tracert_ip_version_flags(monkeypatch):
    from gpm.probers import mtr as M
    monkeypatch.setattr(M, "IS_WINDOWS", True)
    seen = {}

    def fake_run(args, timeout):
        seen["args"] = args
        return 0, "Tracing route to 8.8.8.8 over a maximum of 30 hops\n\n  1    91 ms   91 ms   91 ms  8.8.8.8\n\nTrace complete.\n", ""

    monkeypatch.setattr(M, "run_cmd", fake_run)
    M.run_mtr({"target": "2001:db8::1", "params": {}}, "", "2001:db8::1", None, TS)
    assert "-6" in seen["args"]   # v6 字面量目标 → -6（不能再用历史的 -4）
    M.run_mtr({"target": "8.8.8.8", "params": {"ip_version": "6"}}, "", "8.8.8.8", None, TS)
    assert "-6" in seen["args"]
    M.run_mtr({"target": "8.8.8.8", "params": {}}, "", "8.8.8.8", None, TS)
    assert "-4" in seen["args"] and "-6" not in seen["args"]   # auto 维持历史行为
