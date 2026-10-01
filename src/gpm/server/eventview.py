"""事件详情：时间线 / 影响范围 / 指标曲线 / 统计（只读 storage，不新增 SQL）。

UI 在事件流里点开一条事件时调用本模块。设计约束：

- 只调用 storage 的公开只读方法（incident_get / list_incidents / agg_read /
  agg_buckets_existing / list_tasks / list_nodes / get_task），不写 SQL，
  便于存储层整体替换。
- 唯一会抛出的异常是「事件不存在」的 KeyError；其余取数失败一律退化成缺数据
  （空 series / 空 blast / 计数为 0），避免一条脏数据把详情面板打挂。
- 返回字段名与 bucket 档位是前端契约，改动需同步 UI 与 tests/unit/test_eventview.py。

关键约定（与前端/测试保持一致）：

- 窗口 = [started_at - 10min, (ended_at or ts or now) + 10min]；事件未结束
  （ended_at 为 NULL/0）时用调用方传入的 ts 作为结束，便于历史回放。
- bucket 按「补齐后的窗口跨度」选择：≤3h → 1m，≤3d → 5m，否则 1h。
- series 超过 max_points 时等间隔下采样，必然保留首尾点。
- stats 与 series 取自同一份桶数据，但 stats 在**下采样之前**计算，
  保证样本数/失败数/可用率不被抽样影响。
- blast 的行代表两类影响面：与焦点事件**同任务**的事件归入任务行，
  其余**同节点**的事件归入节点行（先任务后节点，不重复计数）。
"""
from __future__ import annotations

from ..common.util import now

#: 窗口前后各留的余量（秒）：让曲线上能看到事件前后的对照。
PAD_SECONDS = 600

#: bucket 档位阈值（秒，按补齐后的窗口跨度）：≤3h 用 1m，≤3d 用 5m，更大用 1h。
BUCKET_1M_MAX = 3 * 3600
BUCKET_5M_MAX = 3 * 86400

#: 影响范围一次扫描的事件条数上限（storage.list_incidents 的硬上限）。
BLAST_SCAN_LIMIT = 200

#: kind → 中文标签（UI 直接展示）。
KIND_LABELS = {"probe": "探测", "node": "节点侧"}


# ---------------------------------------------------------------- 取值工具

def _get(row, key, default=None):
    """dict 与 sqlite3.Row 都能取值；键不存在时返回 default（NULL 仍是 None）。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return dict(row).get(key, default)
    except Exception:
        return default


def _call(storage, name, *args, default=None, **kwargs):
    """调用 storage 的公开方法；方法缺失或抛错时返回 default（只读接口的容错边界）。"""
    fn = getattr(storage, name, None)
    if not callable(fn):
        return default
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _as_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _kind(inc) -> str:
    k = _get(inc, "kind")
    return str(k) if k else "probe"


def _ended_at(inc):
    """事件结束时间；未结束（NULL/0/空）返回 None。"""
    return _as_int(_get(inc, "ended_at")) or None


def _names(storage, method: str, id_key: str) -> dict:
    out: dict = {}
    for row in _call(storage, method, default=[]) or []:
        rid = _get(row, id_key)
        if rid:
            out[str(rid)] = str(_get(row, "name") or rid)
    return out


def _task_name(storage, task_id: str, names: dict) -> str:
    if not task_id:
        return ""
    if task_id in names:
        return names[task_id]
    task = _call(storage, "get_task", task_id, default=None)
    return str(_get(task, "name") or task_id)


# ---------------------------------------------------------------- 窗口 / bucket

def pick_bucket(t_from: int, t_to: int) -> str:
    """按窗口跨度选择聚合档位（供 detail 与 series_for 复用）。"""
    span = int(t_to) - int(t_from)
    if span <= BUCKET_1M_MAX:
        return "1m"
    if span <= BUCKET_5M_MAX:
        return "5m"
    return "1h"


def _downsample(points: list, max_points) -> list:
    """等间隔下采样，保留首尾点；max_points ≤ 0 表示不返回任何点。"""
    n = len(points)
    if n == 0:
        return []
    try:
        k = int(max_points)
    except (TypeError, ValueError):
        k = 120
    if k <= 0:
        return []
    if n <= k:
        return list(points)
    if k == 1:
        return [points[0]]
    idx = sorted({int(round(i * (n - 1) / (k - 1))) for i in range(k)})
    return [points[i] for i in idx]


def _human_ms(ms) -> str:
    """毫秒 → 中文时长（天/小时/分钟/秒，最多保留两级）。"""
    sec = max(0, int(ms)) // 1000
    day, rem = divmod(sec, 86400)
    hour, rem = divmod(rem, 3600)
    minute, second = divmod(rem, 60)
    if day:
        return f"{day}天{hour}小时"
    if hour:
        return f"{hour}小时{minute}分钟"
    if minute:
        return f"{minute}分钟{second}秒" if second else f"{minute}分钟"
    return f"{second}秒"


# ---------------------------------------------------------------- 指标曲线

def _to_points(rows) -> list[dict]:
    """聚合行 → 曲线点（avail_rate 缺失时按 ok/count 现算）。"""
    out = []
    for r in rows or []:
        ts = _as_int(_get(r, "ts"))
        if ts is None:
            continue
        count = _as_int(_get(r, "count")) or 0
        ok = _as_int(_get(r, "ok"))
        fail = _as_int(_get(r, "fail"))
        if fail is None:
            fail = max(0, count - ok) if ok is not None else 0
        avail = _as_float(_get(r, "avail_rate"))
        if avail is None:
            base = ok if ok is not None else max(0, count - fail)
            avail = round(base / count, 4) if count else None
        out.append({"ts": ts, "avail": avail, "rtt_avg": _as_float(_get(r, "rtt_avg")),
                    "count": count, "fail": max(0, fail)})
    out.sort(key=lambda p: p["ts"])
    return out


def _stream_points(storage, inc, bucket: str, t_from: int, t_to: int) -> list[dict]:
    """流级曲线：task × node × dns × url 精确匹配（事件就是流级）。"""
    rows = _call(storage, "agg_read", bucket, str(_get(inc, "task_id") or ""),
                 str(_get(inc, "node_id") or ""), str(_get(inc, "dns") or ""),
                 str(_get(inc, "url") or ""), int(t_from), int(t_to), default=[]) or []
    return _to_points(rows)


def _task_has_node(task, node_id: str, node_name: str = "") -> bool:
    """任务是否覆盖该节点：nodes 为空视为「全部节点」。

    与 storage.tasks_for_node 一致，nodes 里既可能是节点 id 也可能是节点名；
    组选择（g:<组id>/<组名>）需要分组 API，这里不展开，可能漏掉纯组分配的任务。
    """
    nodes = _get(task, "nodes")
    if not nodes:
        return True
    if isinstance(nodes, str):
        nodes = [nodes]
    targets = {x for x in (node_id, node_name) if x}
    try:
        return any(x in targets for x in nodes)
    except TypeError:
        return True


def _node_points(storage, inc, bucket: str, t_from: int, t_to: int) -> list[dict]:
    """节点侧事件没有 dns/url：用该节点上各任务的桶汇总成一条曲线。

    agg_buckets_existing 只能按任务聚合（无法再按节点过滤），因此只取
    「节点选择覆盖该节点」的任务，跨任务按 count 加权合并；拿不到就返回 []。
    """
    node_id = str(_get(inc, "node_id") or "")
    if not node_id:
        return []
    node_name = _names(storage, "list_nodes", "id").get(node_id, "")
    agg: dict = {}
    for task in _call(storage, "list_tasks", default=[]) or []:
        tid = _get(task, "id")
        if not tid or not _task_has_node(task, node_id, node_name):
            continue
        rows = _call(storage, "agg_buckets_existing", bucket, str(tid),
                     int(t_from), int(t_to), default=[]) or []
        for r in rows:
            ts = _as_int(_get(r, "ts"))
            if ts is None:
                continue
            count = _as_int(_get(r, "count")) or 0
            ok = _as_int(_get(r, "ok"))
            fail = _as_int(_get(r, "fail"))
            if fail is None:
                fail = max(0, count - (ok or 0))
            if ok is None:
                ok = max(0, count - fail)
            slot = agg.setdefault(ts, {"count": 0, "ok": 0, "fail": 0, "w": 0.0, "rtt": 0.0})
            slot["count"] += count
            slot["ok"] += ok
            slot["fail"] += fail
            rtt = _as_float(_get(r, "rtt_avg"))
            if rtt is not None and count > 0:
                slot["w"] += count
                slot["rtt"] += rtt * count
    out = []
    for ts in sorted(agg):
        s = agg[ts]
        out.append({
            "ts": ts,
            "avail": round(s["ok"] / s["count"], 4) if s["count"] else None,
            "rtt_avg": round(s["rtt"] / s["w"], 2) if s["w"] else None,
            "count": s["count"],
            "fail": s["fail"],
        })
    return out


def _points_for(storage, inc, t_from: int, t_to: int) -> list[dict]:
    """窗口内的原始桶序列（未下采样；stats 与 series 共用同一份数据）。"""
    bucket = pick_bucket(t_from, t_to)
    try:
        if _kind(inc) == "node":
            return _node_points(storage, inc, bucket, t_from, t_to)
        return _stream_points(storage, inc, bucket, t_from, t_to)
    except Exception:
        return []


def series_for(storage, inc: dict, t_from: int, t_to: int, max_points: int = 120) -> list[dict]:
    """该流（节点侧事件为「该节点全部流」）在 [t_from, t_to] 内的指标曲线。

    bucket 由窗口跨度决定；点数超过 max_points 时下采样并保留首尾。
    任何取数失败都返回 []，不抛异常。
    """
    try:
        return _downsample(_points_for(storage, inc, t_from, t_to), max_points)
    except Exception:
        return []


# ---------------------------------------------------------------- 统计 / 时间线

def _stats(points: list[dict], inc) -> dict:
    """窗口内统计（基于未下采样的全部桶）。无数据时计数为 0、其余为 None。"""
    samples = sum(p["count"] for p in points)
    fail = sum(p["fail"] for p in points)
    avail = round(max(0, samples - fail) / samples, 4) if samples else None
    weight = sum(p["count"] for p in points if p["rtt_avg"] is not None)
    rtt = (round(sum(p["rtt_avg"] * p["count"] for p in points
                     if p["rtt_avg"] is not None) / weight, 2) if weight else None)
    fails = [p["ts"] for p in points if p["fail"] > 0]
    reason = _get(inc, "reason") or {}
    error_class = _get(reason, "error_class")
    if not error_class:
        error_class = _get(reason, "event")   # 节点侧事件的 reason 只有 event/offline
    return {
        "samples": samples,
        "fail": fail,
        "avail": avail,
        "rtt_avg": rtt,
        "error_class": str(error_class or ""),
        "first_fail_ts": min(fails) if fails else None,
        "last_fail_ts": max(fails) if fails else None,
    }


def _subject(inc, task_name: str, node_name: str) -> str:
    task_id = str(_get(inc, "task_id") or "")
    node_id = str(_get(inc, "node_id") or "")
    if _kind(inc) == "node":
        return f"节点 {node_name or node_id or '未知'}"
    parts = [task_name or task_id or "未知任务", str(_get(inc, "dns") or "") or "默认线路"]
    url = str(_get(inc, "url") or "")
    if url:
        parts.append(url)
    return " · ".join(parts)


def _timeline(inc, task_name: str, node_name: str) -> list[dict]:
    """时间线：open →（recover）→（ack），统一按 ts 升序返回。"""
    events = []
    started = _as_int(_get(inc, "started_at")) or 0
    events.append({"ts": started, "kind": "open",
                   "text": f"事件开始 · {_subject(inc, task_name, node_name)}"})
    ended = _ended_at(inc)
    if ended:
        events.append({"ts": ended, "kind": "recover",
                       "text": f"事件恢复 · 持续 {_human_ms((ended - started) * 1000)}"})
    acked = _as_int(_get(inc, "acked_at")) or 0
    if acked:
        who = str(_get(inc, "acked_by") or "").strip() or "未知用户"
        note = str(_get(inc, "note") or "").strip()
        events.append({"ts": acked, "kind": "ack",
                       "text": f"{who} 已确认" + (f"：{note}" if note else "")})
    events.sort(key=lambda e: e["ts"])
    return events


# ---------------------------------------------------------------- 影响范围

def _overlaps(inc, t_from: int, t_to: int) -> bool:
    """事件与窗口是否有交集（与 storage.list_incidents 的过滤语义一致）。"""
    started = _as_int(_get(inc, "started_at"))
    if started is None:
        return True                      # 数据缺失时不擅自排除
    if started > t_to:
        return False
    ended = _ended_at(inc)
    if ended is None:                    # 未结束：一直延伸到 now，只看起点
        return True
    return ended >= t_from


def blast_radius(storage, inc: dict, t_from: int, t_to: int, limit: int = 10) -> list[dict]:
    """影响范围：窗口内与焦点事件同任务 / 同节点的其它事件聚合。

    - 同任务的事件归入「任务行」，其余同节点的事件归入「节点行」（不重复计数）；
    - 行内附 incidents 计数与 ongoing（组内任一事件未结束即为 True）；
    - kind 取组内最新一条事件的 kind；
    - 按 incidents 降序，其次按任务/节点标识升序，保证输出稳定；limit 截断。
    """
    focal_id = _as_int(_get(inc, "id"))
    task_id = str(_get(inc, "task_id") or "")
    node_id = str(_get(inc, "node_id") or "")
    if not task_id and not node_id:
        return []
    rows = _call(storage, "list_incidents", default=[], limit=BLAST_SCAN_LIMIT,
                 t_from=int(t_from), t_to=int(t_to)) or []
    task_names = _names(storage, "list_tasks", "id")
    node_names = _names(storage, "list_nodes", "id")
    groups: dict = {}
    order: list = []
    for r in rows:
        rid = _as_int(_get(r, "id"))
        if focal_id is not None and rid == focal_id:
            continue                                  # 焦点事件本身不算影响
        if not _overlaps(r, int(t_from), int(t_to)):
            continue                                  # 存储未按窗口过滤时兜底
        r_task = str(_get(r, "task_id") or "")
        r_node = str(_get(r, "node_id") or "")
        if task_id and r_task == task_id:
            key = ("task", task_id)
        elif node_id and r_node == node_id:
            key = ("node", node_id)
        else:
            continue
        group = groups.get(key)
        if group is None:
            group = {"task_id": "", "task_name": "", "node_id": "", "node_name": "",
                     "incidents": 0, "kind": _kind(r), "ongoing": False}
            if key[0] == "task":
                group["task_id"] = task_id
                group["task_name"] = task_names.get(task_id, task_id)
            else:
                group["node_id"] = node_id
                group["node_name"] = node_names.get(node_id, node_id)
            groups[key] = group
            order.append(key)
        group["incidents"] += 1
        if _ended_at(r) is None:
            group["ongoing"] = True
    out = [groups[k] for k in order]
    out.sort(key=lambda g: (-g["incidents"], g["task_id"] or g["node_id"]))
    if limit is not None:
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 10
        out = out[:max(0, lim)]
    return out


# ---------------------------------------------------------------- 详情

def detail(storage, iid: int, ts: int | None = None, max_points: int = 120) -> dict:
    """事件详情：事件本体 + 统计 + 时间线 + 影响范围 + 指标曲线 + 窗口。

    - ts：事件仍在进行时用作「当前时间」（缺省取 now()）；
    - max_points：series 的点数上限（stats 不受其影响）；
    - 事件不存在抛 KeyError("事件不存在")，其余情况不抛异常。
    """
    inc = _call(storage, "incident_get", iid, default=None)
    if inc is None:
        raise KeyError("事件不存在")
    inc = dict(inc)

    started = _as_int(_get(inc, "started_at")) or 0
    ended = _ended_at(inc)
    ref = _as_int(ts)
    if ref is None:
        ref = _as_int(now())
    if ref is None:
        ref = started
    end = ended or ref
    if end < started:
        end = started                    # 时钟回拨/未来 ts 兜底：时长不为负
    t_from = started - PAD_SECONDS
    t_to = end + PAD_SECONDS
    bucket = pick_bucket(t_from, t_to)

    task_id = str(_get(inc, "task_id") or "")
    node_id = str(_get(inc, "node_id") or "")
    task_names = _names(storage, "list_tasks", "id") if task_id else {}
    task_name = _task_name(storage, task_id, task_names)
    node_names = _names(storage, "list_nodes", "id")
    node_name = node_names.get(node_id, node_id) if node_id else ""
    kind_label = KIND_LABELS.get(_kind(inc), KIND_LABELS["probe"])

    points = _points_for(storage, inc, t_from, t_to)
    incident = {
        **inc,
        "task_name": task_name,
        "node_name": node_name,
        "kind_label": kind_label,
        "acked_at": _as_int(_get(inc, "acked_at")) or None,
        "acked_by": str(_get(inc, "acked_by") or ""),
        "note": str(_get(inc, "note") or ""),
        "duration_ms": max(0, end - started) * 1000,
        "ongoing": ended is None,
    }
    return {
        "incident": incident,
        "stats": _stats(points, inc),
        "timeline": _timeline(inc, task_name, node_name),
        "blast": blast_radius(storage, inc, t_from, t_to),
        "series": _downsample(points, max_points),
        "window": {"from": t_from, "to": t_to, "bucket": bucket},
    }
