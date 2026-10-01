"""Agent 解析路径单测：代理 TUN 返回 fake-ip 时用 DoH 升级为真实 IP。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import socket as S

from gpm.agent import agent as A


def _fake_getaddrinfo(ip):
    return lambda *a, **k: [(S.AF_INET, S.SOCK_STREAM, 6, "", (ip, 0))]


def test_fake_ip_upgraded_via_doh(monkeypatch):
    """系统解析到 fake-ip（198.18/198.19）→ 用 DoH 取真实 IP，标签标记来源。"""
    monkeypatch.setattr(S, "getaddrinfo", _fake_getaddrinfo("198.18.0.84"))
    calls = []

    def fake_resolve(host, srv, timeout=2.0, cache=None):
        calls.append(srv)
        return ["103.235.46.96"], 12.5, "doh"

    monkeypatch.setattr(A, "resolve_a", fake_resolve)
    ip, ms, label = A.resolve_for({"target": "jd.com"}, "", A.DnsCache(60, 300), 2.0)
    assert ip == "103.235.46.96" and label.startswith("doh:") and ms == 12.5
    assert calls, "应尝试 DoH 解析"


def test_fake_ip_kept_when_doh_unavailable(monkeypatch):
    """DoH 也失败时保留 fake-ip 并标记为 system（由探测器如实标注，不静默）。"""
    monkeypatch.setattr(S, "getaddrinfo", _fake_getaddrinfo("198.18.0.84"))

    def boom(*a, **k):
        raise A.DnsError("dns_timeout", "DoH 不可用")

    monkeypatch.setattr(A, "resolve_a", boom)
    ip, _ms, label = A.resolve_for({"target": "jd.com"}, "", A.DnsCache(60, 300), 2.0)
    assert ip == "198.18.0.84" and label == "system"


def test_normal_ip_untouched(monkeypatch):
    """正常解析结果不应触发 DoH（避免无谓延迟）。"""
    monkeypatch.setattr(S, "getaddrinfo", _fake_getaddrinfo("223.5.5.5"))
    monkeypatch.setattr(A, "resolve_a", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")))
    ip, _ms, label = A.resolve_for({"target": "example.com"}, "", A.DnsCache(60, 300), 2.0)
    assert ip == "223.5.5.5" and label == "system"


def test_literal_ip_and_dns_line_paths(monkeypatch):
    """字面 IP 直接返回；显式 DNS 线路走 resolve_a。"""
    cache = A.DnsCache(60, 300)
    assert A.resolve_for({"target": "8.8.8.8"}, "", cache, 2.0) == ("8.8.8.8", None, "")
    monkeypatch.setattr(A, "resolve_a", lambda h, srv, timeout=2.0, cache=None: (["1.2.3.4"], 5.0, "udp"))
    ip, ms, label = A.resolve_for({"target": "example.com"}, "223.5.5.5", cache, 2.0)
    assert (ip, ms, label) == ("1.2.3.4", 5.0, "223.5.5.5")
