"""告警规则评估与派发 —— P0 告警闭环的核心。

设计要点（与项目红线一致）：
- 只读聚合表（1m / 1h），**不扫原始结果表**
- 命中维护窗口 → 本轮跳过评估，也不告警
- 同一「规则 + 目标」在 firing 期间不重复打扰：静默期（silence_seconds）内只发一次，
  到期后按需再提醒；条件消失 → 发「已恢复」并把历史里的 firing 标记关闭
- 派发失败只记录到渠道 last_error / 告警 detail，绝不影响主流程
"""
from __future__ import annotations

import logging

try:  # 通知模块由独立模块提供；缺失时降级为「仅记录、不发送」
    from . import notify as _notify
except Exception:  # noqa: BLE001
    _notify = None

log = logging.getLogger("gpm.alerts")

# 指标元信息：中文名 / 默认比较方向 / 单位
METRICS = {
    "avail": ("可用率", "lt", "%", True),
    "rtt_avg": ("延迟均值", "gt", "ms", True),
    "rtt_p95": ("延迟 P95", "gt", "ms", True),
    "loss": ("丢包率", "gt", "%", True),
    "node_offline": ("节点离线", "eq", "", False),
}
OPS = {"lt": "<", "gt": ">", "eq": "=", "ne": "!="}


def _r1(v):
    return None if v is None else round(v, 2)


def window_stats(storage, task_id: str, window_seconds: int, ts: int) -> dict:
    """窗口内聚合（1m / 1h 桶）：可用率、RTT 均值/P95、丢包率。"""
    win = max(60, int(window_seconds or 300))
    bucket = "1m" if win <= 3600 else "1h"
    step = 60 if bucket == "1m" else 3600
    t_to = ts // step * step
    t_from = t_to - win
    rows = storage.agg_buckets_existing(bucket, task_id, t_from, t_to)
    count = sum(int(r["count"] or 0) for r in rows)
    ok = sum(int(r["ok"] or 0) for r in rows)
    # 均值按样本数加权；P95 取窗口内最大值（跨桶 P95 属近似，与项目既有约定一致）
    wr = [(float(r["rtt_avg"]), int(r["count"] or 0)) for r in rows if r["rtt_avg"] is not None]
    wsum = sum(w for _, w in wr) or 0
    p95s = [float(r["rtt_p95"]) for r in rows if r.get("rtt_p95") is not None]
    losses = [(float(r["loss_rate"]) * int(r["count"] or 0), int(r["count"] or 0))
              for r in rows if r.get("loss_rate") is not None]
    lsum = sum(w for _, w in losses) or 0
    return {
        "count": count, "ok": ok, "fail": count - ok,
        "avail": round(ok / count, 4) if count else None,
        "rtt_avg": round(sum(v * w for v, w in wr) / wsum, 2) if wsum else None,
        "rtt_p95": round(max(p95s), 2) if p95s else None,
        "loss": round(sum(v for v, _ in losses) / lsum, 4) if lsum else None,
        "bucket": bucket, "window_seconds": win,
    }


def _compare(value, op: str, threshold: float) -> bool:
    if value is None:
        return False
    if op == "lt":
        return value < threshold
    if op == "gt":
        return value > threshold
    if op == "eq":
        return abs(value - threshold) < 1e-9
    if op == "ne":
        return abs(value - threshold) >= 1e-9
    return False


def _scaled(metric: str, value):
    """可用率/丢包率按百分比展示（0.725 → 72.5）。"""
    if value is None:
        return None
    if metric in ("avail", "loss"):
        return round(value * 100, 2)
    return round(value, 2)


def _fmt(rule: dict, label: str, value, stats: dict, ts: int, kind: str) -> tuple[str, str]:
    from ..common.util import now as _now  # noqa: F401  (保持与全局时间一致)
    metric = rule["metric"]
    name, _d, unit, _agg = METRICS.get(metric, (metric, "gt", "", True))
    shown = _scaled(metric, value)
    when = _time_text(ts)
    if metric == "node_offline":
        head = "节点 " + label + (" 离线" if kind == "firing" else " 已恢复")
        title = ("【告警】" if kind == "firing" else "【恢复】") + head
        body = "\n".join([
            "- 规则：" + rule["name"] + "（" + name + "）",
            "- 节点：" + label,
            "- 时间：" + when,
            "- 说明：节点心跳超时（60s 内无心跳）",
        ])
        return title, body
    if kind == "resolved":
        title = "【恢复】" + label + " " + name + " 已恢复"
    elif kind == "remind":
        title = "【提醒】" + label + " " + name + " 仍未恢复"
    else:
        title = "【告警】" + label + " " + name + " " + str(shown) + unit + " " + OPS.get(rule["op"], "") \
            + " " + str(_scaled(metric, rule["threshold"])) + unit
    body = "\n".join([
        "- 规则：" + rule["name"] + "（" + name + " " + OPS.get(rule["op"], "") + " "
        + str(_scaled(metric, rule["threshold"])) + unit + "，窗口 " + str(stats.get("window_seconds")) + "s）",
        "- 目标：" + label,
        "- 当前值：" + (str(shown) + unit if shown is not None else "无数据"),
        "- 样本：" + str(stats.get("count")) + " 条（成功 " + str(stats.get("ok"))
        + " / 失败 " + str(stats.get("fail")) + "）",
        "- 时间：" + when,
    ])
    return title, body


def _time_text(ts: int) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def flatten(channel: dict) -> dict:
    """存储层是 {type, config:{...}}，通知模块要求扁平字段 → 在这里桥接。"""
    return {"type": channel.get("type"), **(channel.get("config") or {})}


# 失败重投退避（秒）：第 1/2/3 次重试分别等这么久
RETRY_BACKOFF = (60, 300, 900)


def _dispatch(storage, channels: list[dict], title: str, text: str, ts: int):
    """派发到全部渠道，返回 (是否有渠道成功, 渠道数, 成功数, 错误摘要, 失败渠道列表)。"""
    if not channels:
        return False, 0, 0, "未配置可用通知渠道", []
    if _notify is None:
        return False, len(channels), 0, "通知模块（notify.py）不可用", list(channels)
    n_ok, errs, failed = 0, [], []
    for ch in channels:
        try:
            ok, msg = _notify.send(flatten(ch), title, text)
        except Exception as e:  # noqa: BLE001 - 通知失败绝不影响评估
            ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
        storage.channel_touch(ch["id"], ok, "" if ok else msg, ts)
        n_ok += 1 if ok else 0
        if not ok:
            errs.append(ch["name"] + ": " + msg)
            failed.append((ch, msg))
    return n_ok > 0, len(channels), n_ok, "; ".join(errs), failed


def retry_pending(storage, ts: int = 0, limit: int = 10) -> list[dict]:
    """重投队列：把派发失败的通知按退避重试（默认 60s / 300s / 900s，3 次后标记 failed）。"""
    import time
    ts = ts or int(time.time())
    out: list[dict] = []
    if _notify is None:
        return out
    chans = {c["id"]: c for c in storage.list_channels()}
    for row in storage.outbox_due(ts, limit):
        ch = chans.get(row["channel_id"])
        if not ch or not ch["enabled"]:
            storage.outbox_mark(row["id"], "failed", "渠道不存在或已停用")
            out.append({"id": row["id"], "status": "failed", "error": "渠道不存在或已停用"})
            continue
        try:
            ok, msg = _notify.send(flatten(ch), row["title"], row["text"])
        except Exception as e:  # noqa: BLE001
            ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
        attempts = int(row["attempts"] or 0)
        if ok:
            storage.outbox_mark(row["id"], "done")
            storage.channel_touch(ch["id"], True, "", ts)
        elif attempts + 1 >= len(RETRY_BACKOFF):
            storage.outbox_mark(row["id"], "failed", msg)
        else:
            storage.outbox_mark(row["id"], "pending", msg, ts + RETRY_BACKOFF[attempts])
        out.append({"id": row["id"], "status": "done" if ok else ("pending" if attempts + 1 < len(RETRY_BACKOFF) else "failed"),
                    "error": "" if ok else msg})
    return out


def retry_one(storage, oid: int, ts: int = 0) -> tuple[bool, str]:
    """手动强制重投一条（UI「立即重投」按钮），不等退避时间。"""
    import time
    ts = ts or int(time.time())
    row = storage.outbox_get(oid)
    if not row:
        return False, "记录不存在"
    if row["status"] == "done":
        return True, "该通知已成功送达"
    ch = next((c for c in storage.list_channels() if c["id"] == row["channel_id"]), None)
    if not ch:
        storage.outbox_mark(oid, "failed", "渠道不存在")
        return False, "渠道不存在"
    if _notify is None:
        return False, "通知模块不可用"
    try:
        ok, msg = _notify.send(flatten(ch), row["title"], row["text"])
    except Exception as e:  # noqa: BLE001
        ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
    storage.channel_touch(ch["id"], ok, "" if ok else msg, ts)
    if ok:
        storage.outbox_mark(oid, "done")
    else:
        storage.outbox_mark(oid, "pending", msg, ts + RETRY_BACKOFF[0])
    return ok, msg


def push_digest(storage, hours: int = 24, channel_ids: list[str] | None = None,
                ts: int = 0) -> dict:
    """生成巡检摘要并推送（定时调度与 UI 手动推送共用）。"""
    import time
    from . import report
    ts = ts or int(time.time())
    title, text = report.digest_text(storage, hours, ts)
    chans = {c["id"]: c for c in storage.list_channels() if c["enabled"]}
    ids = channel_ids or list(chans)
    channels = [chans[c] for c in ids if c in chans]
    delivered, n_ch, n_ok, err, failed = _dispatch(storage, channels, title, text, ts)
    for ch, msg in failed:
        storage.outbox_add(ts, 0, ch["id"], title, text, ts + RETRY_BACKOFF[0], err=msg)
    storage.setting_set("digest_last_ts", str(ts))
    return {"title": title, "channels": n_ch, "ok": n_ok, "delivered": delivered, "error": err}


def evaluate(storage, ts: int = 0) -> list[dict]:
    """评估一轮全部启用规则；返回本轮产生的事件列表（firing / remind / resolved）。"""
    import time
    ts = ts or int(time.time())
    out: list[dict] = []
    rules = [r for r in storage.list_rules() if r["enabled"]]
    if not rules:
        return out
    chans = {c["id"]: c for c in storage.list_channels() if c["enabled"]}
    tasks = {t["id"]: t for t in storage.list_tasks()}
    nodes = {n["id"]: n for n in storage.list_nodes()}
    changes: list[dict] = []

    for rule in rules:
        metric = rule["metric"]
        keys: list[tuple[str, str]] = []          # [(key, label)]
        if metric == "node_offline":
            ids = [rule["node_id"]] if rule["node_id"] else list(nodes)
            keys = [(nid, nodes.get(nid, {}).get("name", nid)) for nid in ids if nid in nodes]
        else:
            ids = [rule["task_id"]] if rule["task_id"] else [t["id"] for t in tasks.values() if t["enabled"]]
            keys = [(tid, tasks[tid]["name"]) for tid in ids if tid in tasks]

        for key, label in keys:
            t_id = key if metric != "node_offline" else ""
            n_id = key if metric == "node_offline" else ""
            if storage.in_maintenance(ts, task_id=t_id, node_id=n_id):
                continue

            stats = {}
            if metric == "node_offline":
                online = (nodes.get(key, {}).get("status") == "online")
                value = 0.0 if online else 1.0
            else:
                stats = window_stats(storage, key, rule["window_seconds"], ts)
                value = stats.get(metric if metric != "loss" else "loss")
            firing = _compare(value, rule["op"], float(rule["threshold"]))
            opened = storage.alert_open(rule["id"], key)

            if firing:
                last = storage.alert_last(rule["id"], key)
                if last and last["status"] == "firing" and \
                        (ts - int(last["ts"] or 0)) < int(rule["silence_seconds"] or 0):
                    continue                     # 静默期内不重复打扰
                kind = "remind" if opened else "firing"
            else:
                if not opened:
                    continue                     # 无未恢复告警且条件不满足 → 无需动作
                kind = "resolved"

            title, text = _fmt(rule, label, value, stats, ts, kind)
            target = {"task_id": t_id, "node_id": n_id, "label": label,
                      "value": _scaled(metric, value),
                      "threshold": _scaled(metric, rule["threshold"]),
                      "count": stats.get("count"), "ok": stats.get("ok"),
                      "fail": stats.get("fail")}
            changes.append({"rule": rule, "key": key, "label": label, "kind": kind,
                            "title": title, "text": text, "target": target})

    # ---- 聚合派发：同一条规则同一轮里的多个目标合并成一条通知，避免「多节点/多线路刷屏」----
    groups: dict = {}
    for ch in changes:
        dkind = "resolved" if ch["kind"] == "resolved" else "firing"
        groups.setdefault((ch["rule"]["id"], dkind), []).append(ch)

    for (rid, dkind), items in groups.items():
        rule = items[0]["rule"]
        channels = [chans[c] for c in rule["channel_ids"] if c in chans]
        if len(items) == 1:
            send_title, send_text = items[0]["title"], items[0]["text"]
        else:
            head = "【告警】" if dkind == "firing" else "【恢复】"
            names = "、".join(str(i["label"]) for i in items[:6])
            more = "" if len(items) <= 6 else (" 等 " + str(len(items)) + " 个目标")
            send_title = head + rule["name"] + " · " + str(len(items)) + " 个目标同时" + \
                ("异常" if dkind == "firing" else "恢复")
            lines = ["- 规则：" + rule["name"] + "（" + METRICS.get(rule["metric"], (rule["metric"],))[0] + "）",
                     "- 影响：" + str(len(items)) + " 个目标 —— " + names + more,
                     "- 时间：" + _time_text(ts), ""]
            for i in items:
                lines.append("- " + str(i["label"]) + "：" + i["title"].split("】")[-1])
            send_text = "\n".join(lines)
        delivered, n_ch, n_ok, err, failed = _dispatch(storage, channels, send_title, send_text, ts)
        for i in items:
            note = err
            if len(items) > 1:
                note = (note + "；" if note else "") + "聚合发送(" + str(len(items)) + " 个目标)"
            aid = storage.alert_add(ts, rule, i["key"],
                                    "firing" if i["kind"] != "resolved" else "resolved",
                                    i["title"], i["text"], i["target"],
                                    delivered, n_ch, n_ok, note)
            out.append({"id": aid, "kind": i["kind"], "rule": rule["name"], "metric": rule["metric"],
                        "key": i["key"], "label": i["label"], "title": i["title"],
                        "delivered": delivered, "channels": n_ch, "ok": n_ok, "error": err,
                        "grouped": len(items)})
            log.info("告警 %s: %s（%d 个目标聚合，渠道 %d/%d 成功）",
                     i["kind"], i["title"], len(items), n_ok, n_ch)
        # 派发失败的渠道进重投队列（按退避重试，失败也不影响评估）
        for ch, msg in failed:
            storage.outbox_add(ts, 0, ch["id"], send_title, send_text,
                               ts + RETRY_BACKOFF[0], err=msg)
    return out


def test_channel(storage, channel: dict, ts: int = 0) -> tuple[bool, str]:
    """手动测试发送（供 UI「测试发送」按钮）。"""
    import time
    ts = ts or int(time.time())
    title = "【测试】gpm 通知渠道连通性"
    text = "\n".join([
        "- 渠道：" + channel.get("name", channel.get("id", "")),
        "- 时间：" + _time_text(ts),
        "- 说明：收到本条即表示该渠道配置可用。",
    ])
    if _notify is None:
        return False, "通知模块（notify.py）不可用"
    try:
        ok, msg = _notify.send(flatten(channel), title, text)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
    storage.channel_touch(channel["id"], ok, "" if ok else msg, ts)
    return ok, msg
