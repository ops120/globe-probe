"""DNS 解析监控（dns 任务）：单流多线路对比 + 期望命中 + 值变更检测。

- 任务 dns 字段是多条线路列表（udp:/tcp:/dot:/doh:/裸 IP；空串=节点系统解析），
  逐条解析 target 域名，全部结果放进一次上报的 metrics.lines（单流，不按线路拆流）。
- metrics：
    lines = {线路: {ok, answers, ttl, ms} | {ok:false, error, error_class}}
    consistent = 各成功线路的答案集合是否一致（False 仅当 ≥2 条成功且集合不同）
    rtt_ms / rtt_avg = 成功线路解析耗时均值（复用现有 rtt 聚合/告警）
    changed / prev_answers = 值变更检测（agent 侧记忆上次答案集后传入 prev）
- status：≥1 线路解析成功且 expected 命中 → ok；
  全部线路失败 → fail(dns_error)；有解析成功但 expected 不匹配 → fail(expect_mismatch)。
"""
from __future__ import annotations

import ipaddress
import re
import socket
import time

from ..common.dnsres import DnsError, is_fake_ip, resolve_detail
from .base import make_result

# fake-ip（代理 TUN 劫持 A 应答）逐线路升级用的 DoH 兜底（与 agent.resolve_for 同一约定）
_FAKEIP_DOH_FALLBACK = ("223.5.5.5", "119.29.29.29", "8.8.8.8")


def _system_resolve(host: str, timeout: float) -> tuple[list[str], float]:
    """空线路 = 节点系统解析（getaddrinfo，IPv4；与 ping 默认路径同口径）。"""
    t0 = time.monotonic()
    infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise DnsError("dns_servfail", f"系统解析 {host} 无 A 记录")
    return ips, round((time.monotonic() - t0) * 1000, 2)


def _ip_in_expectation(ip: str, expected_ips: list[str]) -> bool:
    """精确 IP 命中或落入任一期望网段（CIDR）。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in expected_ips:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item.strip(), strict=False):
                    return True
            elif ipaddress.ip_address(item.strip()) == addr:
                return True
        except ValueError:
            continue
    return False


def _resolve_line(target: str, line: str, timeout: float,
                  cache) -> tuple[list[str], float, int | None, bool]:
    """单线路解析。返回 (answers, ms, ttl, fake_ip_upgraded)。

    空线路 = 节点系统解析。解析结果全为 fake-ip（代理 TUN 劫持 A 应答）时，
    与 agent.resolve_for 同一约定：用 DoH 兜底取真实答案并如实标注升级。
    DoH 全失败则保留原答案（由 expected/一致性判定如实反映）。
    """
    upgraded = False
    if line:
        ips, ms, _tr, ttl = resolve_detail(target, line, timeout=timeout, cache=cache)
    else:
        ips, ms = _system_resolve(target, timeout)
        ttl = None
    if ips and all(is_fake_ip(ip) for ip in ips):
        for srv in _FAKEIP_DOH_FALLBACK:
            try:
                ips2, ms2, _tr2, ttl2 = resolve_detail(target, f"doh:{srv}",
                                                       timeout=max(timeout, 4.0), cache=cache)
            except DnsError:
                continue
            if ips2 and not all(is_fake_ip(ip) for ip in ips2):
                return ips2, ms2, ttl2, True
    return ips, ms, ttl, upgraded


def answers_hit(answers: list[str], expected_ips: list[str], expected_regex: str) -> bool:
    """期望判定：expected_ips（子网/精确 IP）或 expected_regex（对答案串 search）任一命中。"""
    if expected_ips and any(_ip_in_expectation(ip, expected_ips) for ip in answers):
        return True
    if expected_regex:
        try:
            rx = re.compile(expected_regex)
        except re.error:
            return False
        return any(rx.search(ip) for ip in answers)
    return False


def _norm_lines(dns_lines) -> list[str]:
    """任务 dns 列表 → 逐条线路；空列表视作「节点系统解析」一条。"""
    lines = [str(x or "").strip() for x in (dns_lines or [])]
    return [x for x in lines if x] or [""]


def run_dnsmon(task: dict, cache, ts: int, dns_timeout: float = 2.0,
               prev: list[str] | tuple | None = None) -> dict:
    """task: {target(域名), dns[线路...], params{expected_ips, expected_regex}}。

    prev：agent 侧记忆的上次答案集（该任务成功线路答案的并集，排序后）；
    本次答案集与之不同 → metrics.changed=True + metrics.prev_answers=上次集合。
    """
    target = (task.get("target") or "").strip()
    params = task.get("params") or {}
    expected_ips = [str(x) for x in (params.get("expected_ips") or [])]
    expected_regex = str(params.get("expected_regex") or "")
    lines_spec = _norm_lines(task.get("dns"))

    lines: dict = {}
    ok_answers: set[str] = set()
    ok_ms: list[float] = []
    any_ok = False
    for line in lines_spec:
        label = line or "system"
        try:
            ips, ms, ttl, upgraded = _resolve_line(target, line, dns_timeout, cache)
        except DnsError as e:
            lines[label] = {"ok": False, "error": str(e)[:160], "error_class": e.kind}
            continue
        except OSError as e:
            lines[label] = {"ok": False, "error": f"系统解析失败: {e}"[:160],
                            "error_class": "dns_error"}
            continue
        entry: dict = {"ok": True, "answers": ips, "ttl": ttl, "ms": round(ms, 2)}
        if upgraded:
            entry["fake_ip_upgraded"] = True
        lines[label] = entry
        ok_answers.update(ips)
        ok_ms.append(float(ms))
        any_ok = True

    # 线路集合是否一致：≥2 条成功且答案集合不同 → False
    ok_sets = [set(v.get("answers") or []) for v in lines.values() if v.get("ok")]
    consistent = not (len(ok_sets) >= 2 and any(s != ok_sets[0] for s in ok_sets[1:]))

    metrics: dict = {
        "lines": lines,
        "consistent": consistent,
        "expected": bool(expected_ips or expected_regex),
        "rtt_ms": round(sum(ok_ms) / len(ok_ms), 2) if ok_ms else None,
    }
    metrics["rtt_avg"] = metrics["rtt_ms"]
    cur = sorted(ok_answers)
    metrics["answers"] = cur
    if prev is not None and any_ok:
        prev_set = sorted(set(prev))
        metrics["changed"] = cur != prev_set
        if metrics["changed"]:
            metrics["prev_answers"] = prev_set
    else:
        metrics["changed"] = False

    if not any_ok:
        first: dict = next((v for v in lines.values() if v.get("error_class")), {})
        return make_result(ts, "fail", "dns_error",
                           first.get("error") or "全部线路解析失败", "", "", None, metrics)
    if (expected_ips or expected_regex) and not answers_hit(cur, expected_ips, expected_regex):
        return make_result(ts, "fail", "expect_mismatch",
                           f"答案 {cur[:4]} 不在期望集合", "", "", None, metrics)
    return make_result(ts, "ok", "", "", "", "", None, metrics)
