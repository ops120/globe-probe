"""解析器单元测试 —— fixtures 为真实命令输出样本。

ping 中文样本：2026-10-01 在本机（Windows 11 中文，cp936）对 223.5.5.5 实际执行捕获；
英文样本：标准 Linux/Windows 英文 locale 格式；mtr 文本报告：mtr --report 真实格式。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.probers.base import parse_ping
from gpm.probers.mtr import _parse_text_report, _parse_json, _parse_tracert, judge_path
from gpm.probers.curl import _in_expected
from gpm.common.dnsres import _encode_name, _parse_response, _collect_a, _extract_answers
import struct
import socket

# ---- 真实样本：Windows 中文 ping（本机 2026-10-01 实测捕获）----
PING_ZH = """
正在 Ping 223.5.5.5 具有 32 字节的数据:
请求超时。
来自 223.5.5.5 的回复: 字节=32 时间=14ms TTL=54
来自 223.5.5.5 的回复: 字节=32 时间=13ms TTL=54
来自 223.5.5.5 的回复: 字节=32 时间=8ms TTL=54

223.5.5.5 的 Ping 统计信息:
    数据包: 已发送 = 4，已接收 = 3，丢失 = 1 (25% 丢失)，
往返行程的估计时间(以毫秒为单位):
    最短 = 8ms，最长 = 14ms，平均 = 11ms
"""

# ---- 英文样本（标准格式）----
PING_EN = """
Pinging 1.1.1.1 with 32 bytes of data:
Reply from 1.1.1.1: bytes=32 time=25ms TTL=57
Reply from 1.1.1.1: bytes=32 time=24ms TTL=57
Reply from 1.1.1.1: bytes=32 time<1ms TTL=57
Reply from 1.1.1.1: bytes=32 time=26ms TTL=57

Ping statistics for 1.1.1.1:
    Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 26ms, Average = 18ms
"""

PING_EN_ALL_TIMEOUT = """
Pinging 203.0.113.1 with 32 bytes of data:
Request timed out.
Request timed out.

Ping statistics for 203.0.113.1:
    Packets: Sent = 2, Received = 0, Lost = 2 (100% loss),
"""

PING_ZH_NOHOST = """Ping 请求找不到主机 no-such-host.invalid。请检查该名称，然后重试。"""

PING_LINUX_EN = """
PING 223.5.5.5 (223.5.5.5) 56(84) bytes of data.
64 bytes from 223.5.5.5: icmp_seq=1 ttl=118 time=12.3 ms
64 bytes from 223.5.5.5: icmp_seq=2 ttl=118 time=11.8 ms

--- 223.5.5.5 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 1001ms
rtt min/avg/max/mdev = 11.8/12.0/12.3/0.2 ms
"""

# ---- mtr 真实格式（--report 文本）----
MTR_TEXT = """
Start: 2026-10-01T03:30:00+0800
HOST: bj-ct-01                            Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.31.1                       0.0%    10    2.1   2.0   1.8   2.6   0.2
  2.|-- 100.64.0.1                         0.0%    10    4.5   4.8   4.1   6.0   0.5
  3.|-- 202.97.66.93                       0.0%    10    9.2   9.8   8.9  12.1   0.9
  4.|-- 59.43.182.149                     20.0%    10   41.0  43.5  40.2  52.4   3.8
  5.|-- 8.8.8.8                            0.0%    10   74.2  75.1  72.8  79.9   1.9
"""

MTR_TEXT_LAST_HOP_DEAD = """
Start: 2026-10-01T03:30:00+0800
HOST: bj-ct-01                            Loss%   Snt   Last   Avg  Best  Wrst StDev
  1.|-- 192.168.31.1                       0.0%    10    2.1   2.0   1.8   2.6   0.2
  2.|-- ???                               100.0    10    0.0   0.0   0.0   0.0   0.0
"""

# ---- mtr --json 格式（mtr >=0.94 文档结构）----
MTR_JSON = """{
 "report": {
  "mtr": {"src": "bj-ct-01", "dst": "8.8.8.8", "tests": 10},
  "hubs": [
   {"count": 1, "host": "192.168.31.1", "Loss%": 0.0, "Snt": 10, "Last": 2.1, "Avg": 2.0, "Best": 1.8, "Wrst": 2.6, "StDev": 0.2},
   {"count": 2, "host": "8.8.8.8", "Loss%": 0.0, "Snt": 10, "Last": 74.2, "Avg": 75.1, "Best": 72.8, "Wrst": 79.9, "StDev": 1.9}
  ]
 }
}
"""


def test_parse_ping_zh_real():
    p = parse_ping(PING_ZH, 4)
    assert p["sent"] == 4 and p["received"] == 3
    assert abs(p["loss_rate"] - 0.25) < 1e-9
    assert p["rtt_min"] == 8 and p["rtt_max"] == 14
    assert abs(p["rtt_avg"] - (14 + 13 + 8) / 3) < 0.01
    assert p["error_class"] == ""


def test_parse_ping_en():
    p = parse_ping(PING_EN, 4)
    assert p["sent"] == 4 and p["received"] == 4
    assert p["loss_rate"] == 0
    # time<1ms 也被捕获
    assert p["rtt_min"] == 1.0  # <1ms 解析为 1ms（time<1ms 正则捕获 '1'）
    assert p["error_class"] == ""


def test_parse_ping_linux():
    p = parse_ping(PING_LINUX_EN, 2)
    assert p["sent"] == 2 and p["received"] == 2 and p["loss_rate"] == 0
    assert abs(p["rtt_avg"] - 12.05) < 0.01


def test_parse_ping_all_timeout():
    p = parse_ping(PING_EN_ALL_TIMEOUT, 2)
    assert p["received"] == 0 and p["loss_rate"] == 1.0
    assert p["error_class"] == "timeout"


def test_parse_ping_nohost():
    p = parse_ping(PING_ZH_NOHOST, 4)
    assert p["received"] == 0 and p["error_class"] == "dns_error"


def test_mtr_text_report():
    hops = _parse_text_report(MTR_TEXT)
    assert len(hops) == 5
    assert hops[0]["host"] == "192.168.31.1" and hops[0]["loss_pct"] == 0.0
    assert hops[3]["loss_pct"] == 20.0 and hops[3]["snt"] == 10
    status, cls = judge_path(hops)
    assert status == "ok" and cls == ""      # 中间跳 20% 丢包但末跳正常 → 路径正常


def test_mtr_last_hop_dead():
    hops = _parse_text_report(MTR_TEXT_LAST_HOP_DEAD)
    status, cls = judge_path(hops)
    assert status == "fail" and cls == "path_fail"


def test_mtr_json():
    hops = _parse_json(MTR_JSON)
    assert len(hops) == 2 and hops[1]["avg"] == 75.1
    assert judge_path(hops)[0] == "ok"


def test_curl_expected_status():
    assert _in_expected(200, [[200, 300]])
    assert _in_expected(299, [[200, 300]])
    assert not _in_expected(301, [[200, 300]])
    assert _in_expected(301, [[200, 300], 301])


def test_dns_encode():
    assert _encode_name("www.baidu.com") == b"\x03www\x05baidu\x03com\x00"


def test_dns_parse_wire():
    """构造一个真实的 DNS 响应字节串（header+question+A）验证解析。"""
    qname = _encode_name("www.baidu.com")
    header = struct.pack(">HHHHHH", 0xABCD, 0x8180, 1, 1, 0, 0)
    question = qname + struct.pack(">HH", 1, 1)
    # answer: A 153.3.238.28（名称字段用压缩指针指向 offset 12 的问题名）
    a1 = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 120, 4) + socket.inet_aton("153.3.238.28")
    msg = header + question + a1
    answers = _parse_response(msg, 0xABCD)
    ips, ttl = _collect_a(answers)
    assert ips == ["153.3.238.28"] and ttl == 120


def test_dns_doh_json():
    data = {"Status": 0, "Answer": [
        {"name": "www.baidu.com", "type": 5, "TTL": 300, "data": "www.a.shifen.com."},
        {"name": "www.a.shifen.com", "type": 1, "TTL": 120, "data": "153.3.238.28"}]}
    answers = _extract_answers(data)
    ips, ttl = _collect_a(answers)
    assert ips == ["153.3.238.28"]


def test_validate_target():
    from gpm.common.util import validate_target, validate_url
    assert validate_target("223.5.5.5")
    assert validate_target("www.baidu.com")
    assert validate_target("2001:db8::1")
    assert not validate_target("1.1.1.1; rm -rf /")
    assert not validate_target("$(ping evil)")
    assert not validate_target("")
    assert validate_url("https://www.baidu.com/health")
    assert not validate_url("https://www.baidu.com/health; rm")


# ---- mtr：fake-ip 如实跳过 + 命令超时下限 ----

def test_mtr_fake_ip_is_skipped(monkeypatch):
    """代理 TUN 劫持 DNS 返回 fake-ip 时，ICMP 打不到 —— 直接如实跳过，不白等超时。"""
    from gpm.probers import mtr as M
    monkeypatch.setattr(M, "IS_WINDOWS", False)
    r = M.run_mtr({"target": "jd.com", "params": {"timeout": 10}}, "", "198.18.0.84", None, 100)
    assert r["status"] == "skipped" and r["error_class"] == "fake_ip"
    assert "fake-ip" in r["error"] and "198.18.0.84" in r["error"]
    assert M.run_mtr({"target": "x", "params": {}}, "", "198.19.1.1", None, 100)["error_class"] == "fake_ip"


def test_mtr_command_timeout_floor(monkeypatch):
    """UI 给 mtr 的 params.timeout=10（本意是单次探测超时）不能直接当整条命令超时。"""
    from gpm.probers import mtr as M
    monkeypatch.setattr(M, "IS_WINDOWS", False)
    monkeypatch.setattr(M, "_mtr_version", lambda: (0, 95))
    seen = {}

    def fake_run(args, timeout):
        seen["timeout"] = timeout
        return 0, MTR_JSON, ""

    monkeypatch.setattr(M, "run_cmd", fake_run)
    # cycles=10 → 下限 max(10, 20, 25) = 25s
    r = M.run_mtr({"target": "8.8.8.8", "params": {"timeout": 10, "cycles": 10}},
                  "", "8.8.8.8", None, 100)
    assert seen["timeout"] == 25.0
    assert r["status"] == "ok" and len(r["metrics"]["hops"]) == 2
    # 显式给更大的超时则尊重原值
    M.run_mtr({"target": "8.8.8.8", "params": {"timeout": 60, "cycles": 2}}, "", "8.8.8.8", None, 100)
    assert seen["timeout"] == 60.0


# ---- Windows tracert 解析（真实输出样本，2026-10-01 本机捕获）----

TRACERT_EN = """
Tracing route to 8.8.8.8 over a maximum of 30 hops

  1   288 ms   246 ms   261 ms  8.8.8.8
  2     *        *        *     Request timed out.

Trace complete.
"""

TRACERT_ZH = """
通过最多 30 个跃点跟踪到 8.8.8.8 的路由

  1    <1 ms    <1 ms    <1 ms  192.168.31.1
  2    10 ms    11 ms    10 ms  8.8.8.8
  3     *        *        *     请求超时。

跟踪完成。
"""

TRACERT_OK = """
Tracing route to 106.39.171.134 over a maximum of 30 hops

  1    91 ms   103 ms    82 ms  106.39.171.134

Trace complete.
"""


def test_parse_tracert_en():
    hops = _parse_tracert(TRACERT_EN)
    assert [h["hop"] for h in hops] == [1, 2]
    h0 = hops[0]
    assert h0["host"] == "8.8.8.8" and h0["snt"] == 3 and h0["loss_pct"] == 0
    assert h0["last"] == 261 and h0["best"] == 246 and h0["wrst"] == 288
    assert h0["avg"] == round((288 + 246 + 261) / 3, 3)
    # 全超时跳：主机无法确定 → ???，丢包 100%
    assert hops[1]["host"] == "???" and hops[1]["loss_pct"] == 100.0
    assert judge_path(hops) == ("fail", "path_fail")


def test_parse_tracert_zh_and_ok():
    hops = _parse_tracert(TRACERT_ZH)
    assert [h["hop"] for h in hops] == [1, 2, 3]
    assert hops[0]["host"] == "192.168.31.1" and hops[0]["avg"] == 1.0  # <1ms 记 1ms
    assert hops[2]["host"] == "???" and hops[2]["loss_pct"] == 100.0
    assert judge_path(hops) == ("fail", "path_fail")

    ok = _parse_tracert(TRACERT_OK)
    assert len(ok) == 1 and ok[0]["host"] == "106.39.171.134"
    assert judge_path(ok) == ("ok", "")


def test_parse_tracert_garbage():
    assert _parse_tracert("") == []
    assert _parse_tracert("Tracing route to 8.8.8.8 over a maximum of 30 hops\n\nTrace complete.\n") == []


def test_run_mtr_uses_tracert_on_windows(monkeypatch):
    """Windows 上 mtr 任务降级用 tracert，并把每跳 3 探针如实标注在 metrics.mode。"""
    from gpm.probers import mtr as M
    monkeypatch.setattr(M, "IS_WINDOWS", True)
    seen = {}

    def fake_run(args, timeout):
        seen["args"], seen["timeout"] = args, timeout
        return 0, TRACERT_OK, ""

    monkeypatch.setattr(M, "run_cmd", fake_run)
    r = M.run_mtr({"target": "106.39.171.134", "params": {"timeout": 10}}, "", "106.39.171.134", None, 100)
    assert seen["args"][0] == "tracert" and "-d" in seen["args"]
    assert seen["timeout"] >= 60.0, "UI 传来的 10s 不能直接当整条 tracert 超时"
    assert r["status"] == "ok"
    assert r["metrics"]["mode"] == "tracert" and r["metrics"]["probes_per_hop"] == 3
    assert len(r["metrics"]["hops"]) == 1


# ---- 全路径无响应 vs 末跳黑洞 ----

def test_judge_path_all_dead_is_unreachable():
    """20 跳全 ??? 属于 ICMP 不可达（path_unreachable），不是目标黑洞（path_fail）。"""
    dead = [{"hop": i, "host": "???", "loss_pct": 100.0, "snt": 3, "last": 0.0,
             "avg": 0.0, "best": 0.0, "wrst": 0.0, "stdev": 0.0} for i in range(1, 21)]
    assert judge_path(dead) == ("fail", "path_unreachable")
    # 中间有响应、只有末跳黑洞 → path_fail
    mixed = dead[:19] + [{"hop": 20, "host": "1.2.3.4", "loss_pct": 50.0, "snt": 3, "last": 1.0,
                          "avg": 1.0, "best": 1.0, "wrst": 1.0, "stdev": 0.0}]
    mixed[0] = {"hop": 1, "host": "192.168.1.1", "loss_pct": 0.0, "snt": 3, "last": 1.0,
                "avg": 1.0, "best": 1.0, "wrst": 1.0, "stdev": 0.0}
    assert judge_path(mixed) == ("ok", "")
    assert judge_path([]) == ("fail", "mtr_parse_error")


# ---------------- curl 阶段耗时解析 ----------------

def test_curl_metrics_download_is_rounded():
    """回归：下载耗时是「总计 − 首字节」的差值，必须在原始秒值上相减后取整，
    否则 UI 会显示 0.09000000000000341 这类浮点噪声（用户反馈）。"""
    from gpm.probers.curl import parse_curl_metrics

    out = "200 0.000062 0.004091 0.154390 0.169640 0.169730 2443 153.3.238.28"
    m = parse_curl_metrics(out)
    assert m["download_time"] == 0.09, m          # 不是 0.09000000000000341
    assert m["total_time"] == 169.73 and m["ttfb"] == 169.64
    assert (m["dns_time"], m["connect_time"], m["tls_time"]) == (0.06, 4.09, 154.39)
    assert m["size"] == 2443 and m["remote_ip"] == "153.3.238.28" and m["http_code"] == 200
    for k in ("dns_time", "connect_time", "tls_time", "ttfb", "total_time", "download_time"):
        assert round(m[k], 2) == m[k], (k, m[k])
    # 异常数据（首字节晚于总计）不能让下载为负
    assert parse_curl_metrics("200 0 0 0 0.5 0.4 100 1.1.1.1")["download_time"] == 0.0
    # 残缺/非法输出 → None，不抛异常
    for bad in ("", "garbage", "200 0.1 0.2", None, "200 0.1 0.2 0.3 0.4 0.5"):
        assert parse_curl_metrics(bad) is None, bad
