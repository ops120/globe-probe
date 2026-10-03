"""SLA 报表：区间可用率 / 逐日趋势 / 巡检摘要（只读聚合表）。

接口（签名冻结）::

    sla(storage, t_from, t_to, task_id="", node_id="") -> dict
    daily_series(storage, task_id, days, t_now) -> list[dict]
    digest_text(storage, hours, t_now) -> (标题, 正文 Markdown)

口径与硬约束：

1. 性能红线：数值指标只读聚合表（aggregates 的 1m/5m/1h/1d 桶），不扫 probe_results
   原始表。窗口 <= 2 天用 '1h'，> 2 天用 '1d'；'1d' 无数据时回退 '1h'。
   任务的「流数」用 result_streams()（允许清单内的流枚举接口），不参与数值聚合。
2. avail 一律 0~1 的小数（前端自己 x100）；无数据给 None，绝不给 0。
3. 硬红线：节点离线 != 目标故障。kind='node' 的事件只出现在 incidents.items 里，
   并计入 total/open；downtime_seconds / mttr / mtbf 只统计 kind='probe' 的事件。
4. rtt_avg / loss_rate 按 count 加权平均（各流探测次数不同的近似口径，与 storage 侧
   AVG 的约定一致）；rtt_p95 取各桶最大值；avail 按 SUM(ok)/SUM(count) 重算。
5. 已恢复/进行中的判定：ended_at 为空即「进行中」，其时长算到窗口末尾。
6. MTTR 分段：mtta（触发→首次 ack）/ mttr（ack→恢复），只统计 kind='probe' 且
   已关闭且已 ack 的事件；样本不足（0 条）时值为 None 并在 mtta_note/mttr_note
   给出中文说明；顶层 incidents.mttr_seconds 为旧的「触发→恢复」均值，保持不变。

已知限制：
- list_incidents() 已支持 t_from/t_to（时间过滤下推到 SQL）；
- node_avail(node_id, t_from, task_ids, t_to) 已支持 t_to；事件时长按窗口边界裁剪。
"""
from __future__ import annotations

import time

DAY = 86400
BUCKETS = ("1m", "5m", "1h", "1d")
# list_incidents 没有时间过滤参数，只能按「最近 N 条」扫描后在内存里按窗口裁剪。
_INCIDENT_SCAN_LIMIT = 500

__all__ = ["sla", "daily_series", "digest_text", "DAY", "BUCKETS"]


# ---------------------------------------------------------------- 通用工具

def _get(row, key: str, default=None):
    """宽容取值：dict / sqlite3.Row / 普通对象都吃。"""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return getattr(row, key, default)


def _num(v, default=None):
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f


def _int(v, default: int = 0) -> int:
    n = _num(v)
    return default if n is None else int(n)


def _row_stat(row) -> dict:
    """把一行聚合数据归一化成内部统计。"""
    count = max(0, _int(_get(row, "count")))
    ok = max(0, _int(_get(row, "ok")))
    ok = min(ok, count) if count else ok
    fail = _get(row, "fail")
    fail = max(0, count - ok) if fail is None else max(0, _int(fail))
    return {"ts": _int(_get(row, "ts")), "count": count, "ok": ok, "fail": fail,
            "rtt_avg": _num(_get(row, "rtt_avg")), "rtt_p95": _num(_get(row, "rtt_p95")),
            "loss_rate": _num(_get(row, "loss_rate"))}


def _combine(stats) -> dict:
    """合并若干条统计：count/ok/fail 求和，rtt/loss 按 count 加权，p95 取最大。"""
    stats = list(stats or [])
    count = sum(max(0, _int(s.get("count"))) for s in stats)
    ok = sum(max(0, _int(s.get("ok"))) for s in stats)
    fail = sum(max(0, _int(s.get("fail"))) for s in stats)
    if count:
        ok = min(ok, count)
    rtt_num = rtt_den = 0.0
    loss_num = loss_den = 0.0
    p95 = None
    for s in stats:
        c = max(0, _int(s.get("count")))
        w = c if c > 0 else 1
        v = _num(s.get("rtt_avg"))
        if v is not None:
            rtt_num += v * w
            rtt_den += w
        p = _num(s.get("rtt_p95"))
        if p is not None and (p95 is None or p > p95):
            p95 = p
        lv = _num(s.get("loss_rate"))
        if lv is not None:
            loss_num += lv * w
            loss_den += w
    return {
        "count": count, "ok": ok, "fail": fail,
        "avail": round(ok / count, 4) if count > 0 else None,
        "rtt_avg": round(rtt_num / rtt_den, 2) if rtt_den > 0 else None,
        "rtt_p95": round(p95, 2) if p95 is not None else None,
        "loss_rate": round(loss_num / loss_den, 4) if loss_den > 0 else None,
    }


def _pick_bucket(span_seconds: int) -> str:
    """窗口 <= 2 天用 '1h'，> 2 天用 '1d'（跨天统计优先日桶）。"""
    return "1h" if span_seconds <= 2 * DAY else "1d"


def _read_window(storage, task_id: str, t_from: int, t_to: int, bucket: str):
    """读聚合桶，返回 (rows, 实际读取的 bucket)。'1d' 无数据时回退 '1h'。"""
    # 桶的时间戳是桶**起点**：把读取范围向整桶外扩，否则 t_from/t_to 落在桶中间时
    # 端点桶会被 ts>=t_from / ts<=t_to 过滤掉（表现为「最近 1 小时 0 条」）。
    _step = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}.get(bucket, 3600)
    b_from, b_to = t_from // _step * _step, t_to // _step * _step
    rows = list(storage.agg_buckets_existing(bucket, task_id, b_from, b_to) or [])
    if rows or bucket != "1d":
        return rows, bucket
    rows = list(storage.agg_buckets_existing("1h", task_id, t_from // 3600 * 3600,
                                             t_to // 3600 * 3600) or [])
    return rows, "1h"


def _streams(storage, task_id: str) -> int:
    try:
        return len(storage.result_streams(task_id) or [])
    except Exception:
        return 0


def _node_avail(storage, node_id: str, t_from: int, task_ids, t_to: int = 0) -> dict:
    """节点侧窗口统计：t_to 必须传，否则区间会一直延伸到 now（历史报表会偏大）。"""
    try:
        r = storage.node_avail(node_id, t_from, task_ids, t_to)
    except TypeError:            # 兼容没有 t_to 的旧实现
        r = storage.node_avail(node_id, t_from, task_ids)
    except Exception:
        r = None
    if not r:
        return {"count": 0, "ok": 0}
    return {"count": max(0, _int(_get(r, "count"))), "ok": max(0, _int(_get(r, "ok")))}


# ---------------------------------------------------------------- 事件

def _collect_incidents(storage, t_from: int, t_to: int,
                       task_names: dict, node_names: dict) -> list[dict]:
    """窗口内的事件（按 started_at 倒序）。node 事件保留但不计入停机。"""
    try:
        # 时间过滤下推到 SQL（只取与窗口有交集的事件），避免「只扫最近 N 条」漏掉最旧事件
        raw = storage.list_incidents(limit=_INCIDENT_SCAN_LIMIT, t_from=t_from, t_to=t_to) or []
    except TypeError:                 # 兼容没有时间参数的旧实现
        raw = storage.list_incidents(limit=_INCIDENT_SCAN_LIMIT) or []
    except Exception:
        raw = []
    items: list[dict] = []
    for i in raw:
        started = _int(_get(i, "started_at"))
        ended_raw = _get(i, "ended_at")
        ended = None if ended_raw in (None, 0, "") else _int(ended_raw)
        if started > t_to or (ended is not None and ended < t_from):
            continue                      # 与窗口无交集
        kind = str(_get(i, "kind", "") or "probe")
        # 时长按窗口边界裁剪：跨窗的长事件只计入落在窗口内的部分
        clip_start = max(started, t_from)
        clip_end = t_to if ended is None else min(ended, t_to)
        dur = max(0, clip_end - clip_start) * 1000
        tid = str(_get(i, "task_id", "") or "")
        nid = str(_get(i, "node_id", "") or "")
        task_name = task_names.get(tid) or tid
        node_name = node_names.get(nid) or nid
        if kind == "node":
            title = ("节点离线" if ended is None else "节点恢复") + " · " + node_name
        else:
            title = (task_name or "未知任务") + " · " + str(_get(i, "dns", "") or "默认线路")
        items.append({
            "id": _int(_get(i, "id")),
            "kind": kind,
            "task_id": tid,
            "task_name": task_name,
            "node_id": nid,
            "node_name": node_name,
            "started_at": started,
            "ended_at": ended,
            "duration_ms": dur,
            "title": title,
            "url": str(_get(i, "url", "") or ""),
            "dns": str(_get(i, "dns", "") or ""),
            "reopen_count": _int(_get(i, "reopen_count", 0) or 0),
            "acked_at": _int(_get(i, "acked_at", 0) or 0),
            "acked_by": str(_get(i, "acked_by", "") or ""),
        })
    items.sort(key=lambda x: x["started_at"], reverse=True)
    return items


# ---------------------------------------------------------------- 事件折叠

# 抖动判定：同一目标在这么长的窗口内出现这么多次，就标记为抖动（仍然全部保留，可展开）
FLAP_MIN_COUNT = 3
FLAP_WINDOW_SECONDS = 1800


def _group_incidents(items: list[dict]) -> list[dict]:
    """把重复的同类事件折叠成「目标组」。

    业界做法（Alertmanager 的 group_by + PagerDuty「多告警合并成一个 incident」+
    OneUptime「有界合并窗口、不隐藏根因」）：
    - 分组的键必须是**稳定的服务/依赖标识**（这里 = 事件类型 + 任务 + 节点 + 线路），
      而不是「时间挨得近」；
    - 折叠只影响展示：每条底层事件都保留在 groups[].items 里，可展开、可点进详情；
    - 组上给出 opened_at / last_activity / 累计时长 / 抖动标记，便于快速判断严重程度。
    """
    groups: dict = {}
    for it in items:
        key = (it["kind"], it.get("task_id") or "", it.get("node_id") or "", it.get("dns") or "")
        g = groups.get(key)
        if g is None:
            g = {
                "key": "|".join(key),
                "kind": it["kind"],
                "task_id": it.get("task_id") or "",
                "task_name": it.get("task_name") or "",
                "node_id": it.get("node_id") or "",
                "node_name": it.get("node_name") or "",
                "dns": it.get("dns") or "",
                # 标题带节点名：同一任务+线路在不同节点上是**不同分组**（分组键=任务+节点+线路），
                # 不写节点会出现两行看着一模一样的组，用户无法区分
                "title": (it["title"] + " · " + it["node_name"]) if it.get("node_name") else it["title"],
                "count": 0,
                "first_ts": it["started_at"],
                "last_ts": it["started_at"],
                "downtime_seconds": 0.0,
                "ongoing": False,
                "reopens": 0,
                "streams": set(),
                "items": [],
            }
            groups[key] = g
        g["count"] += 1
        g["first_ts"] = min(g["first_ts"], it["started_at"])
        g["last_ts"] = max(g["last_ts"], it["last_ts"] if "last_ts" in it else
                           (it["ended_at"] if it["ended_at"] else it["started_at"]))
        g["downtime_seconds"] += it["duration_ms"] / 1000.0
        g["ongoing"] = g["ongoing"] or it["ended_at"] is None
        g["reopens"] += int(it.get("reopen_count") or 0)
        g["streams"].add(it.get("url") or "")
        g["items"].append(it)

    out = []
    for g in groups.values():
        g["downtime_seconds"] = int(round(g["downtime_seconds"]))
        g["streams"] = len(g["streams"])
        g["items"].sort(key=lambda x: x["started_at"], reverse=True)
        g["flapping"] = (g["count"] >= FLAP_MIN_COUNT and
                         (g["last_ts"] - g["first_ts"]) <= FLAP_WINDOW_SECONDS)
        out.append(g)
    out.sort(key=lambda x: x["last_ts"], reverse=True)
    return out


# ---------------------------------------------------------------- 对外接口

def _p50(values) -> float | None:
    """中位数（偶数个样本取中间两数均值），保留 1 位小数；空样本返回 None。"""
    xs = sorted(float(v) for v in (values or []))
    n = len(xs)
    if not n:
        return None
    mid = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0
    return round(mid, 1)


def _mtt_segments(probe_items: list[dict]) -> tuple[dict, str, dict, str]:
    """MTTR 分段（ONCALL_OPTIMIZATION P1）：触发→首次确认 / 确认→恢复。

    口径：只统计 kind='probe' 且「已关闭（ended_at 非空）且已 ack（acked_at>0）」
    的事件；MTTA = acked_at - started_at，MTTR = ended_at - acked_at（负值钳为 0）。
    样本不足（0 条）时分段值为 None，并附中文 note 说明原因。
    """
    acked = [x for x in probe_items
             if x["ended_at"] is not None and _int(x.get("acked_at", 0)) > 0]
    mtta_vals = [max(0.0, float(_int(x.get("acked_at", 0)) - x["started_at"])) for x in acked]
    mttr_vals = [max(0.0, float(x["ended_at"] - _int(x.get("acked_at", 0)))) for x in acked]
    mtta = {"p50_s": _p50(mtta_vals),
            "mean_s": round(sum(mtta_vals) / len(mtta_vals), 1) if mtta_vals else None}
    mttr = {"p50_s": _p50(mttr_vals),
            "mean_s": round(sum(mttr_vals) / len(mttr_vals), 1) if mttr_vals else None}
    mtta_note = "" if mtta_vals else "窗口内没有「已确认且已恢复」的探测事件，MTTA 无法计算"
    mttr_note = "" if mttr_vals else "窗口内没有「已确认且已恢复」的探测事件，确认→恢复段无法计算"
    return mtta, mtta_note, mttr, mttr_note


def sla(storage, t_from: int, t_to: int, task_id: str = "", node_id: str = "") -> dict:
    """区间 SLA：整体 / 按任务 / 按节点可用率 + 事件统计。

    数值全部来自聚合表；task_id / node_id 为空表示不做对应过滤。
    窗口内无数据的对象仍会出现在 tasks / nodes 里（count=0、avail=None），
    便于发现「一次都没上报」的静默任务。
    """
    t_from, t_to = int(t_from), int(t_to)
    if t_to < t_from:                     # 防御：参数写反时按区间处理
        t_from, t_to = t_to, t_from
    span = t_to - t_from
    bucket = _pick_bucket(span)

    task_id = str(task_id or "")
    tasks = list(storage.list_tasks() or [])
    if task_id:
        tasks = [t for t in tasks if str(_get(t, "id", "") or "") == task_id]
        if not tasks:                     # 指定了不存在/已删任务：仍然给一行
            tasks = [{"id": task_id, "name": task_id, "type": ""}]
    task_names = {str(_get(t, "id", "") or ""): str(_get(t, "name", "") or "") for t in tasks}

    task_stats: list[dict] = []
    task_rows: list[dict] = []
    for t in tasks:
        tid = str(_get(t, "id", "") or "")
        rows, _ = _read_window(storage, tid, t_from, t_to, bucket)
        st = _combine([_row_stat(r) for r in rows])
        task_stats.append(st)
        task_rows.append({
            "task_id": tid,
            "name": str(_get(t, "name", "") or "") or tid,
            "type": str(_get(t, "type", "") or ""),
            "count": st["count"], "ok": st["ok"], "fail": st["fail"],
            "avail": st["avail"], "rtt_avg": st["rtt_avg"], "rtt_p95": st["rtt_p95"],
            "streams": _streams(storage, tid),
        })

    overall = _combine(task_stats)

    node_id = str(node_id or "")
    nodes = list(storage.list_nodes() or [])
    if node_id:
        nodes = [n for n in nodes if str(_get(n, "id", "") or "") == node_id]
    scope_tasks = [task_id] if task_id else None
    node_rows: list[dict] = []
    for n in nodes:
        nid = str(_get(n, "id", "") or "")
        na = _node_avail(storage, nid, t_from, scope_tasks, t_to)
        count, ok = na["count"], na["ok"]
        status = str(_get(n, "status", "") or "")
        since = _int(_get(n, "online_since"))
        node_rows.append({
            "node_id": nid,
            "name": str(_get(n, "name", "") or "") or nid,
            "status": status,
            "count": count, "ok": ok, "fail": max(0, count - ok),
            "avail": round(ok / count, 4) if count > 0 else None,
            # 即时在线时长（以窗口末尾为「现在」）；离线/无上线时间记 0
            "uptime_seconds": max(0, t_to - since) if status == "online" and since > 0 else 0,
        })

    node_names = {n["node_id"]: n["name"] for n in node_rows}
    items = _collect_incidents(storage, t_from, t_to, task_names, node_names)
    groups = _group_incidents(items)
    probe = [x for x in items if x["kind"] == "probe"]
    recovered = [x for x in probe if x["ended_at"] is not None]
    downtime = int(round(sum(x["duration_ms"] for x in probe) / 1000.0))
    mttr = (round(sum(x["duration_ms"] for x in recovered) / len(recovered) / 1000.0, 1)
            if recovered else None)
    mtbf = round(span / len(probe), 1) if probe else None
    mtta_seg, mtta_note, mttr_seg, mttr_note = _mtt_segments(probe)

    return {
        "window": {"from": t_from, "to": t_to, "hours": span / 3600.0},
        "filter": {"task_id": task_id, "node_id": node_id},
        "overall": overall,
        "tasks": task_rows,
        "nodes": node_rows,
        # MTTR 分段：触发→确认（MTTA）/ 确认→恢复（MTTR），仅统计已关闭且已 ack 的事件
        "mtta": mtta_seg,
        "mtta_note": mtta_note,
        "mttr": mttr_seg,
        "mttr_note": mttr_note,
        "incidents": {
            "total": len(items),
            "open": sum(1 for x in items if x["ended_at"] is None),
            "downtime_seconds": downtime,
            "mttr_seconds": mttr,
            "mtbf_seconds": mtbf,
            "items": items,                      # 平铺（每条事件一行）
            "groups": groups,                    # 折叠：同目标合并成一行
            "group_count": len(groups),
            "flapping_groups": sum(1 for g in groups if g["flapping"]),
        },
    }


def daily_series(storage, task_id: str, days: int, t_now: int) -> list[dict]:
    """逐日趋势（升序，UTC 日界）。

    优先 '1d' 桶；没有日桶（或当天日桶尚未落库）时用 '1h' 桶按天归并。
    缺数据的日期也给行：count=0、avail=None（不要给 0，前端显示「无数据」）。
    task_id 为空表示汇总全部任务。
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 1
    days = max(1, days)
    t_now = int(t_now)
    today = t_now // DAY * DAY
    start = today - (days - 1) * DAY

    acc = {start + i * DAY: {"count": 0, "ok": 0, "fail": 0,
                             "rtt_num": 0.0, "rtt_den": 0.0} for i in range(days)}

    def absorb(rows):
        for r in rows:
            st = _row_stat(r)
            key = st["ts"] // DAY * DAY
            b = acc.get(key)
            if b is None:                 # 落在请求范围外，忽略
                continue
            b["count"] += st["count"]
            b["ok"] += st["ok"]
            b["fail"] += st["fail"]
            if st["rtt_avg"] is not None:
                w = st["count"] or 1
                b["rtt_num"] += st["rtt_avg"] * w
                b["rtt_den"] += w

    task_id = str(task_id or "")
    if task_id:
        tids = [task_id]
    else:
        tids = [str(_get(t, "id", "") or "") for t in (storage.list_tasks() or [])]
        if not tids:
            tids = [task_id]

    for tid in tids:
        rows, used = _read_window(storage, tid, start, t_now, "1d")
        absorb(rows)
        if used == "1d":
            # 日桶通常要等当天结束才落库：当天缺行时用 1h 桶补齐「今天」
            have_today = any(_row_stat(r)["ts"] // DAY * DAY == today for r in rows)
            if not have_today:
                absorb(list(storage.agg_buckets_existing("1h", tid, today, t_now) or []))

    out: list[dict] = []
    for i in range(days):
        ts = start + i * DAY
        b = acc[ts]
        count = b["count"]
        ok = min(b["ok"], count) if count else b["ok"]
        out.append({
            "day": time.strftime("%Y-%m-%d", time.gmtime(ts)),
            "ts": ts,
            "count": count,
            "ok": ok,
            "fail": b["fail"],
            "avail": round(ok / count, 4) if count > 0 else None,
            "rtt_avg": round(b["rtt_num"] / b["rtt_den"], 2) if b["rtt_den"] > 0 else None,
        })
    return out


# ---------------------------------------------------------------- 巡检摘要

def _ts(v: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(v)))


def _pct(v) -> str:
    return "—" if v is None else f"{float(v) * 100:.2f}%"


def _ms(v) -> str:
    return "—" if v is None else f"{float(v):.1f}"


def _secs(v) -> str:
    return "—" if v is None else f"{float(v):.1f} 秒"


def _fmt_hours(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{float(v):g}"


def _fmt_dur(ms) -> str:
    s = max(0, _int(ms)) / 1000.0
    if s >= 86400:
        return f"{s / 86400:.1f} 天"
    if s >= 3600:
        return f"{s / 3600:.1f} 小时"
    if s >= 60:
        return f"{s / 60:.1f} 分钟"
    return f"{s:.0f} 秒"


def _md(text) -> str:
    """表格里的文字：转义竖线、压掉换行。"""
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def digest_text(storage, hours: int, t_now: int) -> tuple[str, str]:
    """巡检摘要：返回 (标题, 正文 Markdown)。

    正文包含：窗口内整体可用率与探测总数、可用率最差的 3 个任务、
    新增/进行中事件数量与最长的 3 条、在线/离线节点数。
    """
    try:
        h = float(hours)
    except (TypeError, ValueError):
        h = 6.0
    if h <= 0:
        h = 1.0
    t_now = int(t_now)
    t_from = t_now - int(round(h * 3600))
    rep = sla(storage, t_from, t_now)
    ov = rep["overall"]
    inc = rep["incidents"]
    nodes = rep["nodes"]

    lines: list[str] = []
    lines.append(f"- 窗口：{_ts(t_from)} ~ {_ts(t_now)} UTC（{_fmt_hours(h)} 小时）")
    lines.append(f"- 整体可用率：{_pct(ov['avail'])}"
                 f"（探测 {ov['count']} 次：成功 {ov['ok']}、失败 {ov['fail']}）")
    lines.append(f"- 延迟：平均 {_ms(ov['rtt_avg'])} ms、P95 {_ms(ov['rtt_p95'])} ms；"
                 f"丢包率：{_pct(ov['loss_rate'])}")

    ranked = [t for t in rep["tasks"] if t["count"] > 0 and t["avail"] is not None]
    ranked.sort(key=lambda t: (t["avail"], -t["count"], t["name"]))
    lines.append("")
    lines.append("### 可用率最差的任务（Top 3）")
    if ranked:
        lines.append("")
        lines.append("| 任务 | 可用率 | 探测数 | 失败 | P95(ms) |")
        lines.append("| --- | ---: | ---: | ---: | ---: |")
        for t in ranked[:3]:
            lines.append(f"| {_md(t['name'])} | {_pct(t['avail'])} | {t['count']} "
                         f"| {t['fail']} | {_ms(t['rtt_p95'])} |")
    else:
        lines.append("")
        lines.append("窗口内没有任务探测数据。")

    longest = sorted(inc["items"], key=lambda x: x["duration_ms"], reverse=True)[:3]
    lines.append("")
    lines.append(f"### 事件（新增/进行中 {inc['total']} 条，其中进行中 {inc['open']} 条）")
    lines.append(f"- 探测类停机合计 {inc['downtime_seconds']} 秒；"
                 f"MTTR {_secs(inc['mttr_seconds'])}；MTBF {_secs(inc['mtbf_seconds'])}")
    if longest:
        lines.append("- 时长最长的 3 条：")
        for it in longest:
            state = "进行中" if it["ended_at"] is None else "已恢复"
            lines.append(f"  - `{_ts(it['started_at'])}` · {_md(it['title'])} · "
                         f"{state} · 持续 {_fmt_dur(it['duration_ms'])}")
    else:
        lines.append("- 窗口内没有新增事件。")

    online = sum(1 for n in nodes if n["status"] == "online")
    offline = sum(1 for n in nodes if n["status"] == "offline")
    other = len(nodes) - online - offline
    tail = f"，其它状态 {other} 个" if other else ""
    lines.append("")
    lines.append("### 节点")
    lines.append(f"- 在线 {online} 个 / 离线 {offline} 个（共 {len(nodes)} 个{tail}）")
    bad = [n for n in nodes if n["status"] != "online"]
    if bad:
        lines.append("- 异常节点：" + "、".join(
            f"{_md(n['name'])}（{n['status'] or '未知'}）" for n in bad[:10]))

    title = f"gpm 巡检报告 · 最近 {_fmt_hours(h)} 小时"
    return title, "\n".join(lines)
