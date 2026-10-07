"""探测器公共层。"""
from __future__ import annotations

import re

from ..common.util import IS_WINDOWS


def make_result(ts: int, status: str, error_class: str = "", error: str = "",
                dns_server: str = "", resolved_ip: str = "", dns_time_ms=None,
                metrics: dict | None = None) -> dict:
    return {
        "ts": ts, "status": status, "error_class": error_class, "error": error,
        "dns_server": dns_server, "resolved_ip": resolved_ip, "dns_time_ms": dns_time_ms,
        "metrics": metrics or {},
    }


_EN_RTT = re.compile(r"[Tt]ime[=<]\s*([\d.]+)\s*ms")
_ZH_RTT = re.compile(r"时间[=<]\s*([\d.]+)\s*ms")
_EN_LOSS = re.compile(r"([\d.]+)%\s*packet loss")
_ZH_LOSS = re.compile(r"丢失\s*=\s*(\d+)\s*\(([\d.]+)%\s*丢失\)")
_ZH_CNT = re.compile(r"已发送\s*=\s*(\d+)，已接收\s*=\s*(\d+)，丢失\s*=\s*(\d+)")
_EN_CNT = re.compile(r"(\d+)\s+packets transmitted, (\d+)\s+(?:packets )?received")
_UNREACHABLE = ("unreachable", "无法访问", "不能访问")
_NOHOST = ("could not find host", "not a known host", "unknown host", "找不到主机", "未知的主机", "未知主机")


def parse_ping(output: str, sent_expected: int) -> dict:
    """解析 ping 输出 → {sent, received, loss_rate, rtt_min, rtt_avg, rtt_max, error_class}。

    兼容英文与中文 Windows/Linux 输出（本机真实样本驱动，见 tests/fixtures）。
    """
    rtts = [float(m) for m in _EN_RTT.findall(output)] or [float(m) for m in _ZH_RTT.findall(output)]
    received = len(rtts)
    sent = sent_expected
    loss_pct = None
    m = _EN_LOSS.search(output)
    if m:
        loss_pct = float(m.group(1))
    else:
        m = _ZH_LOSS.search(output)
        if m:
            loss_pct = float(m.group(2))
    m = _ZH_CNT.search(output)
    if m:
        sent, recv, _lost = int(m.group(1)), int(m.group(2)), int(m.group(3))
        received = max(received, recv) if received else recv
    else:
        m = _EN_CNT.search(output)
        if m:
            sent, recv = int(m.group(1)), int(m.group(2))
            received = max(received, recv) if received else recv
    if loss_pct is not None:
        loss_rate = loss_pct / 100.0
        received = round(sent * (1 - loss_pct / 100.0))
    else:
        loss_rate = (sent - received) / sent if sent else 1.0

    error_class = ""
    low = output.lower()
    if received == 0:
        if any(s in low for s in _NOHOST):
            error_class = "dns_error"
        elif any(s in output for s in _UNREACHABLE):
            error_class = "unreachable"
        else:
            error_class = "timeout"
    return {
        "sent": sent, "received": received, "loss_rate": round(loss_rate, 4),
        "rtt_min": min(rtts) if rtts else None,
        "rtt_avg": round(sum(rtts) / len(rtts), 2) if rtts else None,
        "rtt_max": max(rtts) if rtts else None,
        "error_class": error_class,
    }


def ping_cmd(ip: str, count: int, timeout: float, ip_version: str = "auto") -> list[str]:
    """ping 命令拼装。ip_version ∈ auto|4|6；显式指定时传 -4/-6（双平台都支持），
    auto 维持各平台默认（不传 flag，由系统按目标地址族选择）。"""
    fam = []
    if ip_version == "4":
        fam = ["-4"]
    elif ip_version == "6":
        fam = ["-6"]
    if IS_WINDOWS:
        return ["ping", *fam, "-n", str(count), "-w", str(int(timeout * 1000)), ip]
    return ["ping", *fam, "-c", str(count), "-W", str(max(1, int(timeout))), ip]
