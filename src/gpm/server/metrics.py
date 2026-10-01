"""Prometheus 文本格式指标渲染（纯标准库，version=0.0.4）。

- 只读 storage 的公开方法，不新增 SQL；缺失/NULL 数据省略对应样本行，
  而不是补 0，避免把「采不到」误判成「值为 0」。
- 指标名与标签是 Grafana 面板与测试的契约，改动需同步文档。
- 输出体积有上限（约 200 KiB）：超出后按节点/任务名序截断并附注释说明。
"""
from __future__ import annotations

import math
import time

#: /metrics 响应头使用的内容类型（Prometheus text exposition format 0.0.4）。
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: gpm_config_version 的固定标签值（应用版本，与 pyproject 一致）。
VERSION = "0.1.0"

#: 单次渲染的字节上限（约 200 KiB）；超出后按序截断。
MAX_OUTPUT_BYTES = 200 * 1024

#: 为截断注释预留的字节数，保证最终输出不超过 MAX_OUTPUT_BYTES。
_TRUNCATION_RESERVE = 256

#: ingest 上已知的计数器属性名；其余以 _total 结尾的数值属性也会被导出。
_INGEST_COUNTERS = ("accepted_total", "duplicates_total", "rejected_total",
                    "truncated_total")


# ---------------------------------------------------------------- 取值工具

def _get(row, key, default=None):
    """dict 与 sqlite3.Row 都能取值；仅当键不存在时返回 default（NULL 返回 None）。"""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return dict(row).get(key, default)
    except Exception:
        return default


def _present(row, key) -> bool:
    """判断键是否存在（用于区分「值缺失」与「值为 NULL」）。"""
    try:
        row[key]
        return True
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return key in dict(row)
    except Exception:
        return False


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


def _as_bool_int(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    iv = _as_int(value)
    return 1 if iv else 0


def _escape_label(value) -> str:
    """Prometheus 标签值转义：反斜杠、双引号、换行。"""
    s = "" if value is None else str(value)
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value) -> str | None:
    """数值字面量；无法转换返回 None（调用方省略该行）。保留 4 位小数。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    r = round(f, 4)
    if r == int(r) and abs(r) < 1e15:
        return str(int(r))
    return repr(r)


# ---------------------------------------------------------------- 数据准备

def _prepare_nodes(storage, now_ts: int) -> list[dict]:
    nodes = []
    for n in storage.list_nodes() or []:
        name = _get(n, "name")
        if name in (None, ""):
            name = _get(n, "id", "")
        hb = _as_int(_get(n, "last_heartbeat"))
        since = _as_int(_get(n, "online_since"))
        nodes.append({
            "label": "" if name is None else str(name),
            "up": 1 if str(_get(n, "status", "") or "") == "online" else 0,
            "hb_age": max(0, now_ts - hb) if hb else None,
            "cpu": _as_float(_get(n, "cpu")),
            "mem": _as_float(_get(n, "mem")),
            "uptime": max(0, now_ts - since) if since else None,
        })
    return nodes


def _fallback_avail(storage, task, now_ts: int):
    """storage.list_tasks() 未提供 avail_24h 时，用任务级聚合桶现算（不新增 SQL）。"""
    agg = getattr(storage, "agg_buckets_existing", None)
    tid = _get(task, "id")
    if not callable(agg) or not tid:
        return None
    try:
        buckets = agg("1m", str(tid), now_ts - 86400, now_ts) or []
    except Exception:
        return None
    total = ok = 0
    for b in buckets:
        total += _as_int(_get(b, "count")) or 0
        ok += _as_int(_get(b, "ok")) or 0
    return round(ok / total, 4) if total else None


def _task_streams(task) -> int:
    """流数：优先用存储给出的 streams，否则按 节点×DNS×URL 估算。"""
    if _present(task, "streams"):
        v = _as_int(_get(task, "streams"))
        if v is not None:
            return v
    nodes = _get(task, "nodes") or []
    dns = _get(task, "dns") or []
    urls = _get(task, "urls") or []
    n = len(nodes) if isinstance(nodes, (list, tuple)) else 1
    d = len(dns) if isinstance(dns, (list, tuple)) else 1
    u = len(urls) if isinstance(urls, (list, tuple)) else 1
    return max(1, n) * max(1, d) * max(1, u)


def _prepare_tasks(storage, now_ts: int) -> list[dict]:
    tasks = []
    for t in storage.list_tasks() or []:
        name = _get(t, "name")
        if name in (None, ""):
            name = _get(t, "id", "")
        if _present(t, "avail_24h"):
            avail = _as_float(_get(t, "avail_24h"))
        else:
            avail = _fallback_avail(storage, t, now_ts)
        tasks.append({
            "label": "" if name is None else str(name),
            "type": "" if _get(t, "type") is None else str(_get(t, "type")),
            "enabled": _as_bool_int(_get(t, "enabled")),
            "avail": avail,
            "streams": _as_int(_task_streams(t)),
        })
    return tasks


def _ingest_counters(ingest) -> dict:
    """ingest 上的计数器（getattr 兜底，缺失/非数值一律跳过）。"""
    if ingest is None:
        return {}
    names = list(_INGEST_COUNTERS)
    try:
        for attr in dir(ingest):
            if attr.endswith("_total") and not attr.startswith("_") and attr not in names:
                names.append(attr)
    except Exception:
        pass
    out = {}
    for name in names:
        try:
            value = getattr(ingest, name, None)
        except Exception:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[name] = value
    return out


# ---------------------------------------------------------------- 渲染

def _render(counts, nodes: list[dict], tasks: list[dict], config_ver,
            ingest_counters: dict) -> str:
    out: list[str] = []

    def family(name: str, mtype: str, help_text: str, samples: list[str]):
        if not samples:
            return
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {mtype}")
        out.extend(samples)

    family("gpm_up", "gauge", "1 if the exporter rendered metrics successfully.",
           ["gpm_up 1"])
    family("gpm_config_version", "gauge",
           "Active configuration revision published to agents.",
           [f'gpm_config_version{{version="{_escape_label(VERSION)}"}} '
            f'{_fmt(config_ver) or "0"}'])

    online = sum(1 for n in nodes if n["up"])
    enabled = sum(1 for t in tasks if t["enabled"])
    total_nodes = _as_int(_get(counts, "nodes"))
    total_tasks = _as_int(_get(counts, "tasks"))
    summary = (
        ("gpm_results_total", "Probe results currently stored.",
         _as_int(_get(counts, "results")) or 0),
        ("gpm_nodes_total", "Registered nodes.",
         total_nodes if total_nodes is not None else len(nodes)),
        ("gpm_nodes_online", "Nodes currently online.", online),
        ("gpm_tasks_total", "Configured tasks.",
         total_tasks if total_tasks is not None else len(tasks)),
        ("gpm_tasks_enabled", "Enabled tasks.", enabled),
        ("gpm_incidents_open", "Open incidents.",
         _as_int(_get(counts, "incidents_open")) or 0),
    )
    for name, help_text, value in summary:
        family(name, "gauge", help_text, [f"{name} {value}"])

    for metric, key, help_text in (
        ("gpm_node_up", None, "Node online status (1=online, 0=not online)."),
        ("gpm_node_heartbeat_age_seconds", "hb_age",
         "Seconds since the node last reported a heartbeat."),
        ("gpm_node_cpu_percent", "cpu", "Node CPU usage percent (latest heartbeat)."),
        ("gpm_node_mem_percent", "mem", "Node memory usage percent (latest heartbeat)."),
        ("gpm_node_uptime_seconds", "uptime",
         "Seconds since the node came online (current online streak)."),
    ):
        samples = []
        for n in nodes:
            if key is None:
                value = str(n["up"])
            else:
                value = _fmt(n[key])
                if value is None:
                    continue
            samples.append(f'{metric}{{node="{_escape_label(n["label"])}"}} {value}')
        family(metric, "gauge", help_text, samples)

    avail_samples, stream_samples, enabled_samples = [], [], []
    for t in tasks:
        labels = (f'task="{_escape_label(t["label"])}",'
                  f'type="{_escape_label(t["type"])}"')
        av = _fmt(t["avail"])
        if av is not None:
            avail_samples.append(f"gpm_task_availability_24h{{{labels}}} {av}")
        sv = _fmt(t["streams"])
        if sv is not None:
            stream_samples.append(f"gpm_task_streams{{{labels}}} {sv}")
        enabled_samples.append(
            f'gpm_task_enabled{{task="{_escape_label(t["label"])}"}} {t["enabled"]}')
    family("gpm_task_availability_24h", "gauge",
           "Task availability over the last 24 hours (success/total).", avail_samples)
    family("gpm_task_streams", "gauge", "Number of probe streams per task.", stream_samples)
    family("gpm_task_enabled", "gauge", "Task enabled status (1=enabled, 0=disabled).",
           enabled_samples)

    for name in sorted(ingest_counters):
        value = _fmt(ingest_counters[name])
        if value is None:
            continue
        family(f"gpm_ingest_{name}", "counter",
               f"Ingest counter {name}.", [f"gpm_ingest_{name} {value}"])

    return "\n".join(out) + "\n"


def render(storage, ingest=None, now_ts: int | None = None) -> str:
    """输出 Prometheus 文本格式（text/plain; version=0.0.4; charset=utf-8）。"""
    ts = int(time.time()) if now_ts is None else int(now_ts)
    counts = storage.stats_counts() or {}
    config_ver = storage.config_version()
    nodes = _prepare_nodes(storage, ts)
    tasks = _prepare_tasks(storage, ts)
    ingest_counters = _ingest_counters(ingest)

    full = _render(counts, nodes, tasks, config_ver, ingest_counters)
    if len(full.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return full

    # 超限：二分搜索「节点/任务各保留前 k 个」的最大 k（按存储返回顺序，稳定可复现）。
    def build(k: int) -> str:
        return _render(counts, nodes[:k], tasks[:k], config_ver, ingest_counters)

    lo, hi = 0, max(len(nodes), len(tasks))
    best, best_k = build(0), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = build(mid)
        if len(candidate.encode("utf-8")) + _TRUNCATION_RESERVE <= MAX_OUTPUT_BYTES:
            best, best_k = candidate, mid
            lo = mid + 1
        else:
            hi = mid - 1

    kept_nodes = min(len(nodes), best_k)
    kept_tasks = min(len(tasks), best_k)
    note = (f"# gpm: output truncated at {MAX_OUTPUT_BYTES // 1024} KiB; "
            f"kept {kept_nodes}/{len(nodes)} nodes and {kept_tasks}/{len(tasks)} tasks")
    return best + note + "\n"
