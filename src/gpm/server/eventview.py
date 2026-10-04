"""事件详情：时间线 / 影响范围 / 指标曲线 / 统计（只读 storage，不新增 SQL）。

UI 在事件流里点开一条事件时调用本模块。设计约束：

- 只调用 storage 的公开只读方法（incident_get / list_incidents / agg_read /
  agg_buckets_existing / agg_node_cells / list_tasks / list_nodes / get_task /
  audit_list / dns_answer_changes / node_metrics），不写 SQL，
  便于存储层整体替换；本模块新增的取数全部走带 LIMIT 的 storage 方法。
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

故障快速定位四块（detail 返回新增的可选键，只加键不改既有键）：

- changes:   [{ts,who,action,detail}] —— audit_log 事件窗口 ±30min 内的写操作，
             ≤8 条按 ts 倒序；空态返回单条 {"action":"none", detail=说明文字}。
- dns_changes: [{ts,answers,changed}] —— 同目标域名的 dns 任务近 24h 解析值变更，
             ≤5 条倒序；空态返回单条 {"ts":0, answers:[], changed:false, note=说明}。
- scope_matrix: {"nodes":[{node_name,cells:[{ts,st}]}], "verdict":verdict dict} ——
             目标×节点矩阵（每格 = 该节点在事件窗内该分钟的状态 ok/fail），
             结论复用 diagnose.verdict；窗口内无数据时 verdict.verdict 给说明文字。
- dying:     [{ts,cpu,mem}] —— 仅 kind=node 事件携带（probe 事件无此键）：
             last_heartbeat 前 30 分钟心跳资源（按分钟桶）；空态单条带 note。
"""
from __future__ import annotations

import urllib.parse

from ..common.util import now
from ..config import Config
from . import diagnose as _diagnose

# ---------------------------------------------------------------- 配置

#: 模块级 cfg 句柄（由 app.create_app 在启动时通过 init_cfg 注入）。
#: _cfg 未注入时回落到模块私有默认值，保留测试与离线调用兼容性。
_cfg: Config | None = None


def init_cfg(cfg: Config) -> None:
    """由 create_app 调用一次；之后模块内函数通过 _v() 读取 cfg.view。"""
    global _cfg
    _cfg = cfg


def _v(key: str, default):
    """读 cfg.view[key]；cfg 未注入或键缺失时回落到默认。"""
    if _cfg is not None:
        try:
            return _cfg.view[key]
        except (KeyError, TypeError, AttributeError):
            pass
    return default


# —— 口径默认值（运行时一律经 _v() 实时读 cfg.view，禁止在 import 期冻结成模块常量）——
# 曾经这批值在 import 时就求值成 PAD_SECONDS/CHANGES_PAD_SECONDS 等「公开常量」：
# init_cfg() 注入发生在 import 之后，那些常量永远是默认值——看起来可配、实际恒默认的
# 假接口，谁引用谁踩坑。如今只保留带 _ 前缀的默认值，消费方全部走 _v()。

#: 窗口前后各留的余量（秒）：让曲线上能看到事件前后的对照。
_PAD_SECONDS = 600

#: bucket 档位阈值（秒，按补齐后的窗口跨度）：≤3h 用 1m，≤3d 用 5m，更大用 1h。
_BUCKET_1M_MAX = 3 * 3600
_BUCKET_5M_MAX = 3 * 86400

#: 影响范围一次扫描的事件条数上限（storage.list_incidents 的硬上限）。
_BLAST_SCAN_LIMIT = 200

#: kind → 中文标签（UI 直接展示）。
KIND_LABELS = {"probe": "探测", "node": "节点侧"}

# ---------------- 故障快速定位四块的口径 ----------------

#: 同期变更窗口：事件窗口前后各 30 分钟（audit_log）。
_CHANGES_PAD_SECONDS = 1800
#: 同期变更最多条数。
_CHANGES_LIMIT = 8
#: 同期变更一次扫描的审计条数上限（storage.audit_list 的 LIMIT）。
_CHANGES_SCAN_LIMIT = 64
#: DNS 变更联动窗口（秒）：事件结束前 24 小时。
_DNS_CHANGES_WINDOW = 86400
#: DNS 变更联动最多条数。
_DNS_CHANGES_LIMIT = 5
#: 范围矩阵每节点最多格数（等间隔下采样，保留首尾）。
_MATRIX_MAX_CELLS = 60
#: 范围矩阵节点行数上限（防御异常规模的节点表）。
_MATRIX_MAX_NODES = 32
#: 临终曲线窗口：last_heartbeat 前 30 分钟。
_DYING_WINDOW_SECONDS = 1800
#: 临终曲线最多点数（30 分钟 × 每分钟 1 点 = 30，留余量）。
_DYING_MAX_POINTS = 60


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
    if span <= _v("bucket_1m_max_seconds", _BUCKET_1M_MAX):
        return "1m"
    if span <= _v("bucket_5m_max_seconds", _BUCKET_5M_MAX):
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
    rows = _call(storage, "list_incidents", default=[],
                 limit=_v("blast_scan_limit", _BLAST_SCAN_LIMIT),
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


# ---------------------------------------------------------------- 定位四块

def _empty_changes(note: str) -> list[dict]:
    return [{"ts": 0, "who": "", "action": "none", "detail": note}]


def _empty_dns_changes(note: str) -> list[dict]:
    return [{"ts": 0, "answers": [], "changed": False, "note": note}]


def _empty_dying(note: str) -> list[dict]:
    return [{"ts": 0, "cpu": None, "mem": None, "note": note}]


def _empty_matrix(note: str) -> dict:
    return {"nodes": [],
            "verdict": {"mode": "", "failed": 0, "total": 0, "failed_names": [],
                        "verdict": note, "advice": ""}}


def _domain_of(task) -> str:
    """从任务配置提取目标域名：target 优先，其次 urls 第一条；提取不到返回空。"""
    if task is None:
        return ""
    candidates = [str(_get(task, "target") or "")]
    urls = _get(task, "urls")
    if isinstance(urls, list):
        candidates += [str(u or "") for u in urls]
    for raw in candidates:
        v = raw.strip()
        if not v:
            continue
        host = urllib.parse.urlparse(v).hostname if "://" in v else v.split("/")[0]
        host = str(host or "").split("@")[-1].split(":")[0].strip()
        if host:
            return host
    return ""


def _is_ip(host: str) -> bool:
    """近似判断 host 是不是 IP 字面量（IPv4 点分 / 含冒号的 IPv6）。"""
    h = (host or "").strip()
    if ":" in h:
        return True
    parts = h.split(".")
    return (len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts))


def _changes(storage, started: int, end: int) -> list[dict]:
    """同期变更：audit_log 在事件窗口 ±30min 内的写操作，≤8 条按 ts 倒序。"""
    pad = _v("changes_pad_seconds", _CHANGES_PAD_SECONDS)
    t_from = int(started) - pad
    t_to = int(end) + pad
    rows = _call(storage, "audit_list", default=[],
                 limit=_v("changes_scan_limit", _CHANGES_SCAN_LIMIT),
                 since=t_from) or []
    picked: list[dict] = []
    for r in rows:
        ts = _as_int(_get(r, "ts"))
        if ts is None or ts < t_from or ts > t_to:
            continue
        bits = [str(_get(r, "target") or ""), str(_get(r, "target_id") or "")]
        extra = str(_get(r, "detail") or "").strip()
        if extra:
            bits.append(extra)
        picked.append({"ts": ts,
                       "who": str(_get(r, "who") or ""),
                       "action": str(_get(r, "action") or ""),
                       "detail": " · ".join(b for b in bits if b)})
    if not picked:
        return _empty_changes("事件窗口 ±30 分钟内没有操作审计记录")
    picked.sort(key=lambda x: x["ts"], reverse=True)
    return picked[:_v("changes_limit_detail", _CHANGES_LIMIT)]


def _dns_changes(storage, inc, task, end: int) -> list[dict]:
    """DNS 变更联动：同目标域名的 dns 任务在事件结束前 24h 内的解析值变更（≤5 条）。"""
    if _kind(inc) == "node":
        return _empty_dns_changes("节点侧事件没有目标域名，不联动 DNS 变更")
    domain = _domain_of(task)
    if not domain:
        return _empty_dns_changes("目标不是域名，无 DNS 解析变更可联动")
    if _is_ip(domain):
        return _empty_dns_changes("目标是 IP 地址，不经过域名解析，无 DNS 变更可联动")
    win = _v("dns_changes_window_seconds", _DNS_CHANGES_WINDOW)
    limit = _v("dns_changes_limit", _DNS_CHANGES_LIMIT)
    rows = _call(storage, "dns_answer_changes", domain, int(end) - win,
                 int(end), limit, default=[]) or []
    if not rows:
        return _empty_dns_changes(
            f"近 24 小时内 {domain} 的 DNS 解析无变更（或没有同域名 dns 任务）")
    out = []
    for r in rows[:limit]:
        answers = _get(r, "answers")
        out.append({"ts": _as_int(_get(r, "ts")) or 0,
                    "answers": [str(a) for a in answers] if isinstance(answers, list) else [],
                    "changed": True})
    return out


def _scope_matrix(storage, inc, task, t_from: int, t_to: int) -> dict:
    """范围矩阵：目标×节点在事件窗口内的状态格 + diagnose.verdict 三档结论。

    - 节点行 = 事件所属任务覆盖的节点（nodes 为空视为全部节点，与 blast 同约定），
      上限 view.matrix_max_nodes 行；每行 cells = 该节点窗口内各桶的 ok/fail，
      超过 view.matrix_max_cells 格时等间隔下采样（保留首尾）。
    - verdict 复用 diagnose.verdict：节点状态取「窗内任一格 fail 即 fail」；
      窗口内完全没有数据时 verdict.verdict 给出说明文字。
    """
    task_id = str(_get(inc, "task_id") or "")
    if not task_id:
        return _empty_matrix("节点侧事件没有目标任务，无范围矩阵")
    bucket = pick_bucket(t_from, t_to)
    rows = _call(storage, "agg_node_cells", bucket, task_id, int(t_from), int(t_to),
                 default=[]) or []
    by_node: dict = {}
    for r in rows:
        nid = str(_get(r, "node_id") or "")
        ts = _as_int(_get(r, "ts"))
        count = _as_int(_get(r, "count")) or 0
        if not nid or ts is None or count <= 0:
            continue
        fail = _as_int(_get(r, "fail"))
        if fail is None:
            ok = _as_int(_get(r, "ok"))
            fail = max(0, count - (ok or 0))
        by_node.setdefault(nid, []).append({"ts": ts, "st": "fail" if fail > 0 else "ok"})
    node_names = _names(storage, "list_nodes", "id")
    nodes_out: list[dict] = []
    states: list[dict] = []
    # 任务未覆盖的节点不进矩阵（含只有聚合数据但不在任务节点清单里的）；nodes 空=全部节点
    candidates = [nid for nid in sorted(by_node, key=lambda x: node_names.get(x, x))
                  if _task_has_node(task, nid, node_names.get(nid, nid))]
    for n in _call(storage, "list_nodes", default=[]) or []:
        nid = str(_get(n, "id") or "")
        if nid and nid not in candidates and _task_has_node(task, nid, node_names.get(nid, nid)):
            candidates.append(nid)
    for nid in candidates[:_v("matrix_max_nodes", _MATRIX_MAX_NODES)]:
        cells = by_node.get(nid, [])
        cells.sort(key=lambda c: c["ts"])
        cells = _downsample(cells, _v("matrix_max_cells", _MATRIX_MAX_CELLS))
        nodes_out.append({"node_name": node_names.get(nid, nid), "cells": cells})
        if any(c["st"] == "fail" for c in cells):
            st = "fail"
        elif any(c["st"] == "ok" for c in cells):
            st = "ok"
        else:
            st = "skipped"
        states.append({"node_name": node_names.get(nid, nid), "status": st})
    v = _diagnose.verdict(states)
    if not v["mode"]:
        v["verdict"] = "事件窗口内没有该目标的节点探测数据，无法给出范围结论"
    return {"nodes": nodes_out, "verdict": v}


def _dying(storage, inc, started: int):
    """临终曲线：kind=node 事件取 last_heartbeat 前 30 分钟的 CPU/内存（按分钟桶）。

    probe 事件返回 None（detail 不带 dying 键）；无心跳数据返回带说明的空态单条。
    """
    if _kind(inc) != "node":
        return None
    node_id = str(_get(inc, "node_id") or "")
    reason = _get(inc, "reason")
    hb = _as_int(_get(reason, "last_heartbeat")) if isinstance(reason, dict) else None
    if not hb:
        hb = int(started) or 0
    rows = _call(storage, "node_metrics", node_id,
                 hb - _v("dying_window_seconds", _DYING_WINDOW_SECONDS), hb, 60,
                 default=[]) or []
    pts = []
    for r in rows:
        ts = _as_int(_get(r, "ts"))
        if ts is None:
            continue
        pts.append({"ts": ts, "cpu": _as_float(_get(r, "cpu")),
                    "mem": _as_float(_get(r, "mem"))})
    if not pts:
        return _empty_dying("离线前 30 分钟内没有该节点的心跳资源数据")
    pts.sort(key=lambda p: p["ts"])
    return pts[:_v("dying_max_points", _DYING_MAX_POINTS)]


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
    pad = _v("eventview_pad_seconds", _PAD_SECONDS)
    t_from = started - pad
    t_to = end + pad
    bucket = pick_bucket(t_from, t_to)

    task_id = str(_get(inc, "task_id") or "")
    node_id = str(_get(inc, "node_id") or "")
    task_names = _names(storage, "list_tasks", "id") if task_id else {}
    task_name = _task_name(storage, task_id, task_names)
    node_names = _names(storage, "list_nodes", "id")
    node_name = node_names.get(node_id, node_id) if node_id else ""
    kind_label = KIND_LABELS.get(_kind(inc), KIND_LABELS["probe"])
    task = _call(storage, "get_task", task_id, default=None) if task_id else None

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
    out = {
        "incident": incident,
        "stats": _stats(points, inc),
        "timeline": _timeline(inc, task_name, node_name),
        "blast": blast_radius(storage, inc, t_from, t_to),
        "series": _downsample(points, max_points),
        "window": {"from": t_from, "to": t_to, "bucket": bucket},
    }

    # ---- 故障快速定位四块（只加键，不改既有键；任何取数失败退化为空态说明）----
    try:
        out["changes"] = _changes(storage, started, end)
    except Exception:  # noqa: BLE001
        out["changes"] = _empty_changes("同期变更数据读取失败")
    try:
        out["dns_changes"] = _dns_changes(storage, inc, task, end)
    except Exception:  # noqa: BLE001
        out["dns_changes"] = _empty_dns_changes("DNS 变更数据读取失败")
    try:
        out["scope_matrix"] = _scope_matrix(storage, inc, task, started, end)
    except Exception:  # noqa: BLE001
        out["scope_matrix"] = _empty_matrix("范围矩阵数据读取失败")
    try:
        dying = _dying(storage, inc, started)
    except Exception:  # noqa: BLE001
        dying = _empty_dying("节点心跳资源数据读取失败")
    if dying is not None:
        out["dying"] = dying            # probe 事件无此键（契约约定）
    return out
