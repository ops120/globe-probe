"""eventview 单元测试：自写假 storage，不联网、不启服务、不碰 SQLite。

覆盖：detail 结构与统计、ongoing 事件用 ts 收尾、bucket 三档与 max_points
下采样（保留首尾）、时间线顺序与文本、影响范围聚合/排序/limit/窗口过滤、
节点侧事件分支、以及「取数失败不抛异常」的兜底。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server import eventview  # noqa: E402

BASE = 1_700_000_000
DNS = "223.5.5.5"
URL = "https://example.com/probe"


# ---------------------------------------------------------------- 构造工具

def _inc(iid, started, ended=None, **kw):
    d = {
        "id": iid, "task_id": "t1", "node_id": "n1", "dns": DNS, "url": URL,
        "started_at": started, "ended_at": ended,
        "duration_ms": (ended - started) * 1000 if ended else None,
        "kind": "probe", "reason": {"error_class": "timeout", "fail_streak": 3},
        "acked_at": 0, "acked_by": "", "note": "",
    }
    d.update(kw)
    return d


def _row(ts, count=10, ok=10, rtt_avg=10.0, avail_rate=None):
    return {"ts": ts, "count": count, "ok": ok, "fail": count - ok,
            "rtt_avg": rtt_avg, "rtt_p95": rtt_avg, "avail_rate": avail_rate}


def _in_window(inc, t_from, t_to):
    """与 storage.list_incidents 相同的「与窗口有交集」语义。"""
    if t_from and not (inc["ended_at"] is None or inc["ended_at"] >= t_from):
        return False
    if t_to and (inc["started_at"] or 0) > t_to:
        return False
    return True


class FakeStorage:
    """只实现 eventview 会用到的只读方法。"""

    def __init__(self, incidents=(), tasks=(), nodes=(), aggs=None, buckets=None,
                 ignore_window=False, audit=(), dns_changes=None, node_cells=None,
                 heartbeats=None):
        self.incidents = [dict(i) for i in incidents]
        self.tasks = [dict(t) for t in tasks]
        self.nodes = [dict(n) for n in nodes]
        self.aggs = dict(aggs or {})         # (bucket,task,node,dns,url) -> rows
        self.buckets = dict(buckets or {})   # task_id -> 任务级桶
        self.ignore_window = ignore_window
        self.audit = [dict(a) for a in audit]
        self.dns_changes = [dict(r) for r in (dns_changes or [])]
        self.node_cells = [dict(r) for r in (node_cells or [])]
        self.heartbeats = [dict(h) for h in (heartbeats or [])]
        self.agg_calls = []
        self.bucket_calls = []
        self.audit_calls = []
        self.dns_calls = []
        self.cell_calls = []
        self.hb_calls = []

    def incident_get(self, iid):
        for i in self.incidents:
            if int(i["id"]) == int(iid):
                return dict(i)
        return None

    def list_incidents(self, limit=30, open_only=False, t_from=0, t_to=0):
        out = [dict(i) for i in self.incidents
               if not (open_only and i.get("ended_at"))
               and (self.ignore_window or _in_window(i, t_from, t_to))]
        out.sort(key=lambda i: i.get("started_at") or 0, reverse=True)
        return out[:limit]

    def list_tasks(self):
        return [dict(t) for t in self.tasks]

    def list_nodes(self):
        return [dict(n) for n in self.nodes]

    def get_task(self, tid):
        for t in self.tasks:
            if t["id"] == tid:
                return dict(t)
        return None

    def agg_read(self, bucket, task_id, node_id, dns, url, t_from, t_to):
        self.agg_calls.append((bucket, task_id, node_id, dns, url, t_from, t_to))
        rows = self.aggs.get((bucket, task_id, node_id, dns, url), [])
        return [dict(r) for r in rows if t_from <= r["ts"] <= t_to]

    def agg_buckets_existing(self, bucket, task_id, t_from, t_to):
        self.bucket_calls.append((bucket, task_id, t_from, t_to))
        rows = self.buckets.get(task_id, [])
        return [dict(r) for r in rows if t_from <= r["ts"] <= t_to]

    # ---- 定位四块的数据源 ----

    def audit_list(self, limit=100, action="", target="", since=0):
        self.audit_calls.append((limit, since))
        rows = [dict(a) for a in self.audit if a.get("ts", 0) >= since]
        rows.sort(key=lambda a: a.get("ts", 0), reverse=True)
        return rows[:limit]

    def dns_answer_changes(self, domain, t_from, t_to, limit=5):
        self.dns_calls.append((domain, t_from, t_to, limit))
        return [dict(r) for r in self.dns_changes if t_from <= r.get("ts", 0) <= t_to][:limit]

    def agg_node_cells(self, bucket, task_id, t_from, t_to, limit=2048):
        self.cell_calls.append((bucket, task_id, t_from, t_to))
        rows = [dict(r) for r in self.node_cells
                if r.get("ts", 0) and t_from <= r["ts"] <= t_to]
        return rows[:limit]

    def node_metrics(self, node_id, t_from, t_to, bucket=300):
        self.hb_calls.append((node_id, t_from, t_to, bucket))
        return [dict(h) for h in self.heartbeats
                if h.get("node_id") == node_id and t_from <= h.get("ts", 0) <= t_to]


def _stream_key(bucket, task_id="t1", node_id="n1", dns=DNS, url=URL):
    return (bucket, task_id, node_id, dns, url)


def _full_storage():
    """一条已恢复的探测事件 + 3 个 1m 桶（含 avail_rate 缺失的一桶）。"""
    inc = _inc(1, BASE, BASE + 600)
    rows = [_row(BASE + 60, count=10, ok=10, rtt_avg=20.0, avail_rate=1.0),
            _row(BASE + 120, count=10, ok=5, rtt_avg=40.0, avail_rate=0.5),
            _row(BASE + 180, count=4, ok=2, rtt_avg=None, avail_rate=None)]
    return FakeStorage(incidents=[inc],
                       tasks=[{"id": "t1", "name": "ping-223"}],
                       nodes=[{"id": "n1", "name": "北京-1"}],
                       aggs={_stream_key("1m"): rows})


# ---------------------------------------------------------------- detail 结构

def test_detail_structure_and_fields():
    out = eventview.detail(_full_storage(), 1)
    assert set(out) == {"incident", "stats", "timeline", "blast", "series", "window",
                        "changes", "dns_changes", "scope_matrix"}
    assert set(out["stats"]) == {"samples", "fail", "avail", "rtt_avg", "error_class",
                                 "first_fail_ts", "last_fail_ts"}
    assert set(out["window"]) == {"from", "to", "bucket"}
    assert set(out["series"][0]) == {"ts", "avail", "rtt_avg", "count", "fail"}

    inc = out["incident"]
    assert inc["id"] == 1 and inc["started_at"] == BASE and inc["dns"] == DNS
    assert inc["task_name"] == "ping-223"
    assert inc["node_name"] == "北京-1"
    assert inc["kind_label"] == "探测"
    assert inc["ongoing"] is False
    assert inc["duration_ms"] == 600_000
    assert inc["acked_at"] is None and inc["acked_by"] == "" and inc["note"] == ""
    assert inc["reason"]["error_class"] == "timeout"          # 原始字段保留

    assert out["window"] == {"from": BASE - 600, "to": BASE + 1200, "bucket": "1m"}

    st = out["stats"]
    assert st["samples"] == 24
    assert st["fail"] == 7
    assert st["avail"] == round(17 / 24, 4)
    assert st["rtt_avg"] == 30.0
    assert st["error_class"] == "timeout"
    assert st["first_fail_ts"] == BASE + 120
    assert st["last_fail_ts"] == BASE + 180

    series = out["series"]
    assert [p["ts"] for p in series] == [BASE + 60, BASE + 120, BASE + 180]
    assert series[0]["avail"] == 1.0 and series[0]["rtt_avg"] == 20.0
    assert series[1]["avail"] == 0.5 and series[1]["fail"] == 5
    assert series[2]["avail"] == 0.5 and series[2]["rtt_avg"] is None   # 缺 avail_rate 时现算
    assert out["blast"] == []
    assert [e["kind"] for e in out["timeline"]] == ["open", "recover"]


def test_detail_missing_incident_raises_keyerror():
    with pytest.raises(KeyError) as ei:
        eventview.detail(_full_storage(), 999)
    assert ei.value.args[0] == "事件不存在"
    with pytest.raises(KeyError):
        eventview.detail(object(), 1)      # storage 没有 incident_get 也算「不存在」


def test_detail_ongoing_uses_ts_as_end():
    store = _full_storage()
    store.incidents.append(_inc(2, BASE, None))
    out = eventview.detail(store, 2, ts=BASE + 300)
    assert out["incident"]["ongoing"] is True
    assert out["incident"]["ended_at"] is None
    assert out["incident"]["duration_ms"] == 300_000
    assert out["window"]["to"] == BASE + 300 + 600
    assert out["window"]["bucket"] == "1m"
    assert [e["kind"] for e in out["timeline"]] == ["open"]   # 未恢复：无 recover
    assert out["stats"]["samples"] == 24                     # 窗口内桶照常统计


def test_detail_ongoing_without_ts_falls_back_to_now():
    store = _full_storage()
    store.incidents.append(_inc(2, BASE, None))
    out = eventview.detail(store, 2)
    assert out["incident"]["ongoing"] is True
    assert out["window"]["to"] > BASE + 600
    assert out["incident"]["duration_ms"] > 0


# ---------------------------------------------------------------- bucket / 下采样

@pytest.mark.parametrize("duration,bucket,samples", [
    (1 * 3600, "1m", 1),
    (9600, "1m", 1),          # 补齐后跨度恰好 3h
    (9601, "5m", 2),          # 3h + 1s
    (2 * 86400, "5m", 2),
    (258000, "5m", 2),        # 补齐后跨度恰好 3d
    (258001, "1h", 3),        # 3d + 1s
    (4 * 86400, "1h", 3),
])
def test_bucket_selection_by_window(duration, bucket, samples):
    inc = _inc(1, BASE, BASE + duration)
    aggs = {_stream_key("1m"): [_row(BASE + 60, count=1, ok=1)],
            _stream_key("5m"): [_row(BASE + 60, count=2, ok=2)],
            _stream_key("1h"): [_row(BASE + 60, count=3, ok=3)]}
    store = FakeStorage(incidents=[inc], tasks=[{"id": "t1", "name": "A"}],
                        nodes=[{"id": "n1", "name": "N1"}], aggs=aggs)
    out = eventview.detail(store, 1)
    assert out["window"]["bucket"] == bucket
    assert store.agg_calls and store.agg_calls[0][0] == bucket   # 只读所选档位
    assert out["stats"]["samples"] == samples
    assert out["series"][0]["count"] == samples


def _many_bucket_storage(n=500, step=300, count=2):
    """2 天窗口（5m 档）+ 500 个桶，用于验证下采样。"""
    started = BASE
    inc = _inc(1, started, BASE + 2 * 86400)
    rows = [_row(started + i * step, count=count,
                 ok=count if i % 2 == 0 else count - 1, rtt_avg=10.0 + i)
            for i in range(n)]
    store = FakeStorage(incidents=[inc], tasks=[{"id": "t1", "name": "A"}],
                        nodes=[{"id": "n1", "name": "N1"}],
                        aggs={_stream_key("5m"): rows})
    return store, started, rows


def test_series_downsample_keeps_first_and_last():
    store, started, rows = _many_bucket_storage()
    out = eventview.detail(store, 1, max_points=120)
    series = out["series"]
    assert len(series) == 120 and len(series) <= 120
    assert series[0]["ts"] == started
    assert series[-1]["ts"] == rows[-1]["ts"]
    ts = [p["ts"] for p in series]
    assert ts == sorted(set(ts))                     # 升序且无重复
    # stats 基于全部 500 个桶，不受下采样影响
    assert out["stats"]["samples"] == 1000
    assert out["stats"]["fail"] == 250
    assert out["stats"]["rtt_avg"] == 259.5
    assert out["window"]["bucket"] == "5m"


@pytest.mark.parametrize("max_points,expected", [(1, 1), (3, 3), (250, 250), (1000, 500)])
def test_series_max_points_variants(max_points, expected):
    store, started, rows = _many_bucket_storage()
    series = eventview.detail(store, 1, max_points=max_points)["series"]
    assert len(series) == expected
    assert series[0]["ts"] == started
    if expected > 1:
        assert series[-1]["ts"] == rows[-1]["ts"]


def test_series_max_points_zero_returns_empty():
    store, _, _ = _many_bucket_storage()
    out = eventview.detail(store, 1, max_points=0)
    assert out["series"] == []
    assert out["stats"]["samples"] == 1000           # 统计仍然完整


def test_series_for_and_blast_radius_accept_plain_dict():
    store, started, rows = _many_bucket_storage()
    inc = store.incident_get(1)
    series = eventview.series_for(store, inc, BASE - 600, BASE + 2 * 86400 + 600,
                                  max_points=7)
    assert len(series) == 7
    assert series[0]["ts"] == started and series[-1]["ts"] == rows[-1]["ts"]
    assert eventview.blast_radius(store, inc, BASE - 600, BASE + 1200) == []


# ---------------------------------------------------------------- 时间线

def test_timeline_open_recover_ack():
    inc = _inc(1, BASE, BASE + 600, acked_at=BASE + 900, acked_by="ops",
               note="已联系运营商")
    store = FakeStorage(incidents=[inc], tasks=[{"id": "t1", "name": "ping-223"}],
                        nodes=[{"id": "n1", "name": "北京-1"}])
    out = eventview.detail(store, 1)
    tl = out["timeline"]
    assert [e["kind"] for e in tl] == ["open", "recover", "ack"]
    assert [e["ts"] for e in tl] == [BASE, BASE + 600, BASE + 900]
    assert tl[0]["text"].startswith("事件开始 · ")
    assert "ping-223" in tl[0]["text"] and DNS in tl[0]["text"] and URL in tl[0]["text"]
    assert tl[1]["text"] == "事件恢复 · 持续 10分钟"
    assert tl[2]["text"] == "ops 已确认：已联系运营商"
    assert out["incident"]["acked_at"] == BASE + 900
    assert out["incident"]["acked_by"] == "ops"
    assert out["incident"]["note"] == "已联系运营商"

    # 只写备注、未确认 → 不产生 ack 事件
    store.incidents.append(_inc(2, BASE, BASE + 600, note="占位备注"))
    assert [e["kind"] for e in eventview.detail(store, 2)["timeline"]] == ["open", "recover"]


# ---------------------------------------------------------------- 影响范围

def _blast_storage(ignore_window=False):
    incidents = [
        _inc(1, BASE, BASE + 600),                                             # 焦点
        _inc(2, BASE + 60, BASE + 300, task_id="t1", node_id="n2", dns="d1"),  # 同任务
        _inc(3, BASE + 120, None, task_id="t1", node_id="n3", dns="d1"),       # 同任务、进行中
        _inc(4, BASE + 180, BASE + 240, task_id="t9", node_id="n1", dns="d2"), # 同节点、别的任务
        _inc(5, BASE - 6000, BASE - 5000, task_id="t1", node_id="n4", dns="d1"),  # 窗口外(早)
        _inc(6, BASE + 99999, BASE + 100000, task_id="t1", node_id="n5"),         # 窗口外(晚)
        _inc(7, BASE + 300, BASE + 400, task_id="t7", node_id="n7", dns="d9"),    # 无关
    ]
    return FakeStorage(
        incidents=incidents,
        tasks=[{"id": "t1", "name": "ping-A"}, {"id": "t9", "name": "curl-B"},
               {"id": "t7", "name": "other"}],
        nodes=[{"id": "n1", "name": "北京-1"}, {"id": "n2", "name": "上海-1"},
               {"id": "n3", "name": "广州-1"}, {"id": "n7", "name": "东京-1"}],
        ignore_window=ignore_window)


def test_blast_radius_groups_sorted_and_limited():
    out = eventview.detail(_blast_storage(), 1)["blast"]
    assert set(out[0]) == {"task_id", "task_name", "node_id", "node_name",
                           "incidents", "kind", "ongoing"}
    assert [r["incidents"] for r in out] == [2, 1]        # 按计数降序
    assert sum(r["incidents"] for r in out) == 3          # 窗口外/无关事件被排除

    task_row, node_row = out
    assert task_row["task_id"] == "t1" and task_row["task_name"] == "ping-A"
    assert task_row["node_id"] == "" and task_row["node_name"] == ""
    assert task_row["kind"] == "probe" and task_row["ongoing"] is True
    assert node_row["node_id"] == "n1" and node_row["node_name"] == "北京-1"
    assert node_row["task_id"] == "" and node_row["incidents"] == 1
    assert node_row["ongoing"] is False


def test_blast_radius_limit():
    store = _blast_storage()
    inc = store.incident_get(1)
    t_from, t_to = BASE - 600, BASE + 1200
    assert [r["task_id"] for r in eventview.blast_radius(store, inc, t_from, t_to, 1)] == ["t1"]
    assert len(eventview.blast_radius(store, inc, t_from, t_to, 1)) == 1
    assert eventview.blast_radius(store, inc, t_from, t_to, 0) == []
    assert len(eventview.blast_radius(store, inc, t_from, t_to, None)) == 2


def test_blast_radius_excludes_out_of_window_events_when_storage_ignores_window():
    store = _blast_storage(ignore_window=True)
    out = eventview.detail(store, 1)["blast"]
    assert sum(r["incidents"] for r in out) == 3          # 兜底窗口过滤在 eventview 内生效


def test_blast_radius_for_node_incident():
    focal = _inc(1, BASE, BASE + 600, task_id="", node_id="n1", kind="node",
                 dns="", url="", reason={"event": "offline"})
    store = FakeStorage(
        incidents=[focal,
                   _inc(2, BASE + 60, BASE + 120, task_id="", node_id="n1", kind="node",
                        dns="", url=""),
                   _inc(3, BASE + 60, BASE + 120, task_id="t9", node_id="n1", dns="d3"),
                   _inc(4, BASE + 60, BASE + 120, task_id="t8", node_id="n8", dns="d4")],
        tasks=[{"id": "t9", "name": "curl-B"}],
        nodes=[{"id": "n1", "name": "北京-1"}])
    out = eventview.detail(store, 1)["blast"]
    assert len(out) == 1
    row = out[0]
    assert row["node_id"] == "n1" and row["node_name"] == "北京-1"
    assert row["task_id"] == "" and row["incidents"] == 2
    assert row["kind"] == "node" and row["ongoing"] is False


# ---------------------------------------------------------------- 节点侧事件

def test_node_kind_series_merges_streams_of_node():
    focal = _inc(1, BASE, BASE + 600, task_id="", node_id="n1", kind="node",
                 dns="", url="", reason={"event": "offline", "node": "北京-1"})
    store = FakeStorage(
        incidents=[focal],
        tasks=[{"id": "t1", "name": "A", "nodes": ["n1"]},
               {"id": "t2", "name": "B", "nodes": ["n2"]},
               {"id": "t3", "name": "C", "nodes": []}],
        nodes=[{"id": "n1", "name": "北京-1"}],
        buckets={"t1": [_row(BASE + 60, count=5, ok=5, rtt_avg=10.0),
                        _row(BASE + 120, count=5, ok=4, rtt_avg=20.0)],
                 "t2": [_row(BASE + 60, count=100, ok=100, rtt_avg=99.0)],
                 "t3": [_row(BASE + 60, count=1, ok=1, rtt_avg=20.0)]})
    out = eventview.detail(store, 1)
    assert out["incident"]["kind_label"] == "节点侧"
    assert out["incident"]["task_name"] == ""
    assert out["incident"]["node_name"] == "北京-1"
    assert [c[1] for c in store.bucket_calls] == ["t1", "t3"]    # t2 不在该节点上
    assert store.agg_calls == []                                 # 节点侧不走agg_read
    assert out["stats"]["samples"] == 11
    assert out["stats"]["fail"] == 1
    assert out["stats"]["avail"] == round(10 / 11, 4)
    # 节点侧每个桶的 rtt 先四舍五入到 2 位再加权：(11.67*6 + 20*5)/11
    assert out["stats"]["rtt_avg"] == 15.46
    assert out["stats"]["first_fail_ts"] == BASE + 120
    assert [p["ts"] for p in out["series"]] == [BASE + 60, BASE + 120]
    assert out["series"][0]["count"] == 6 and out["series"][0]["rtt_avg"] == 11.67
    assert out["timeline"][0]["text"] == "事件开始 · 节点 北京-1"


def test_node_kind_matches_task_by_node_name():
    """任务的 nodes 可能写节点名（见 storage.tasks_for_node），也要算进来。"""
    focal = _inc(1, BASE, BASE + 600, task_id="", node_id="n1", kind="node", dns="", url="")
    store = FakeStorage(
        incidents=[focal],
        tasks=[{"id": "t1", "name": "A", "nodes": ["北京-1"]},   # 节点名
               {"id": "t2", "name": "B", "nodes": ["g:华东"]},   # 组选择：不展开，跳过
               {"id": "t3", "name": "C", "nodes": ["n2"]}],      # 别的节点
        nodes=[{"id": "n1", "name": "北京-1"}],
        buckets={"t1": [_row(BASE + 60, count=3, ok=3)],
                 "t2": [_row(BASE + 60, count=50, ok=50)],
                 "t3": [_row(BASE + 60, count=50, ok=50)]})
    out = eventview.detail(store, 1)
    assert [c[1] for c in store.bucket_calls] == ["t1"]
    assert out["stats"]["samples"] == 3


def test_node_kind_without_aggregates_returns_empty_series():
    focal = _inc(1, BASE, BASE + 600, task_id="", node_id="n1", kind="node",
                 dns="", url="", reason={"event": "offline"})
    store = FakeStorage(incidents=[focal], tasks=[{"id": "t1", "name": "A"}],
                        nodes=[{"id": "n1", "name": "北京-1"}])
    out = eventview.detail(store, 1)
    assert out["series"] == []
    assert out["stats"]["samples"] == 0 and out["stats"]["fail"] == 0
    assert out["stats"]["avail"] is None and out["stats"]["rtt_avg"] is None
    assert out["stats"]["first_fail_ts"] is None
    assert out["stats"]["last_fail_ts"] is None
    assert out["stats"]["error_class"] == "offline"     # reason 只有 event 时用它兜底


def test_stats_without_data():
    store = FakeStorage(incidents=[_inc(1, BASE, BASE + 600)],
                        tasks=[{"id": "t1", "name": "A"}],
                        nodes=[{"id": "n1", "name": "N1"}])
    out = eventview.detail(store, 1)
    assert out["stats"] == {"samples": 0, "fail": 0, "avail": None, "rtt_avg": None,
                            "error_class": "timeout", "first_fail_ts": None,
                            "last_fail_ts": None}
    assert out["series"] == [] and out["blast"] == []


class _BrokenStorage:
    """所有取数都炸：detail 必须退化为缺数据而不是抛异常。"""

    def __init__(self, inc):
        self._inc = inc

    def incident_get(self, iid):
        return dict(self._inc)

    def list_tasks(self):
        raise RuntimeError("boom")

    def list_nodes(self):
        raise RuntimeError("boom")

    def agg_read(self, *a, **kw):
        raise RuntimeError("boom")

    def list_incidents(self, *a, **kw):
        raise RuntimeError("boom")


def test_detail_never_raises_on_broken_storage():
    inc = _inc(1, BASE, BASE + 600, task_id="t9", node_id="n9")
    out = eventview.detail(_BrokenStorage(inc), 1)
    assert out["series"] == [] and out["blast"] == []
    assert out["stats"]["samples"] == 0
    assert out["incident"]["task_name"] == "t9"    # 名称取不到时退化为 id
    assert out["incident"]["node_name"] == "n9"
    assert [e["kind"] for e in out["timeline"]] == ["open", "recover"]
    assert out["window"]["bucket"] == "1m"


# ---------------------------------------------------------------- 定位四块

def _audit(ts, who="admin", action="停用任务", target_id="t1", detail=""):
    return {"ts": ts, "who": who, "action": action, "target": "任务", "target_id": target_id,
            "status": 200, "ip": "10.0.0.9", "detail": detail}


def test_changes_block_window_order_and_limit():
    audits = [_audit(BASE + 100, action="停用任务", detail="curl-A"),
              _audit(BASE - 1200, who="ops", action="修改节点", target_id="n2"),
              _audit(BASE - 999999),                            # 窗口外（太早）
              _audit(BASE + 999999)] + \
             [_audit(BASE + 50 + i) for i in range(6)]          # 撑到 8 条上限
    store = FakeStorage(incidents=[_inc(1, BASE, BASE + 600)],
                        tasks=[{"id": "t1", "name": "ping-223"}],
                        nodes=[{"id": "n1", "name": "北京-1"}], audit=audits)
    out = eventview.detail(store, 1)["changes"]
    # 上限 = view.changes_limit_detail（默认 8）。曾经引用 import 期冻结的
    # eventview.CHANGES_LIMIT 常量——那是 init_cfg 注入前求值的假接口，已删除
    assert len(out) == 8
    assert [c["ts"] for c in out] == sorted((c["ts"] for c in out), reverse=True)
    assert out[0]["ts"] == BASE + 100 and out[0]["who"] == "admin"
    assert out[0]["action"] == "停用任务" and "t1" in out[0]["detail"] and "curl-A" in out[0]["detail"]
    assert BASE - 1200 in {c["ts"] for c in out}               # 窗口内 ±30min 都算
    assert BASE - 999999 not in {c["ts"] for c in out}
    # 空态：给明确说明文字
    empty = eventview.detail(FakeStorage(incidents=[_inc(2, BASE, BASE + 600)],
                                         tasks=[{"id": "t1", "name": "A"}]), 2)["changes"]
    assert empty[0]["ts"] == 0 and empty[0]["action"] == "none"
    assert "没有操作审计记录" in empty[0]["detail"]


def test_dns_changes_block():
    task = {"id": "t1", "name": "curl-A", "target": "https://example.com/x",
            "urls": ["https://example.com/x"]}
    changes = [{"ts": BASE - 3600, "answers": ["1.1.1.1"], "prev_answers": ["2.2.2.2"],
                "task_id": "tdns", "task_name": "dns-A"}]
    store = FakeStorage(incidents=[_inc(1, BASE, BASE + 600)], tasks=[task],
                        nodes=[{"id": "n1", "name": "北京-1"}], dns_changes=changes)
    out = eventview.detail(store, 1)["dns_changes"]
    assert store.dns_calls[0][0] == "example.com"              # 从 target 提取域名
    assert store.dns_calls[0][1] == BASE + 600 - 86400         # 24h 窗口锚定在事件结束
    assert out == [{"ts": BASE - 3600, "answers": ["1.1.1.1"], "changed": True}]

    # 无变更 → 空态说明
    out2 = eventview.detail(FakeStorage(incidents=[_inc(1, BASE, BASE + 600)], tasks=[task]),
                            1)["dns_changes"]
    assert out2[0]["changed"] is False and "无变更" in out2[0]["note"]
    # 目标是 IP → 说明（不经过域名解析）
    ip_task = {"id": "t2", "name": "ping-ip", "target": "223.5.5.5"}
    out3 = eventview.detail(FakeStorage(incidents=[_inc(3, BASE, BASE + 600, task_id="t2")],
                                        tasks=[ip_task]), 3)["dns_changes"]
    assert "IP 地址" in out3[0]["note"]
    # 节点侧事件 → 说明
    node_inc = _inc(4, BASE, BASE + 600, task_id="", node_id="n1", kind="node",
                    dns="", url="", reason={"event": "offline"})
    out4 = eventview.detail(FakeStorage(incidents=[node_inc]), 4)["dns_changes"]
    assert "没有目标域名" in out4[0]["note"]


def test_scope_matrix_block():
    cells = [{"ts": BASE + 60, "node_id": "n1", "count": 6, "ok": 0, "fail": 6},
             {"ts": BASE + 120, "node_id": "n1", "count": 6, "ok": 0, "fail": 6},
             {"ts": BASE + 60, "node_id": "n2", "count": 6, "ok": 6, "fail": 0}]
    store = FakeStorage(incidents=[_inc(1, BASE, BASE + 600)],
                        tasks=[{"id": "t1", "name": "ping-223", "nodes": []}],
                        nodes=[{"id": "n1", "name": "北京-1"}, {"id": "n2", "name": "上海-1"},
                               {"id": "n3", "name": "广州-1"}],
                        node_cells=cells)
    out = eventview.detail(store, 1)["scope_matrix"]
    assert store.cell_calls[0][0] == "1m" and store.cell_calls[0][1] == "t1"
    # 行按节点名排序（中文名按码点）；有数据的节点在前、无数据节点补在后面
    assert [n["node_name"] for n in out["nodes"]] == ["上海-1", "北京-1", "广州-1"]
    assert out["nodes"][1]["cells"] == [{"ts": BASE + 60, "st": "fail"},
                                        {"ts": BASE + 120, "st": "fail"}]
    assert out["nodes"][0]["cells"] == [{"ts": BASE + 60, "st": "ok"}]
    assert out["nodes"][2]["cells"] == []                      # 无数据节点也在矩阵里（灰行）
    v = out["verdict"]
    assert v["mode"] == "single_node" and v["failed"] == 1 and v["total"] == 2
    assert v["verdict"].startswith("仅单节点失败")
    # 任务限定了 nodes → 未覆盖的节点不进矩阵
    store2 = FakeStorage(incidents=[_inc(1, BASE, BASE + 600)],
                         tasks=[{"id": "t1", "name": "A", "nodes": ["n1"]}],
                         nodes=[{"id": "n1", "name": "北京-1"}, {"id": "n2", "name": "上海-1"}],
                         node_cells=cells)
    out2 = eventview.detail(store2, 1)["scope_matrix"]
    assert [n["node_name"] for n in out2["nodes"]] == ["北京-1"]
    # 节点侧事件没有目标任务 → 说明
    node_inc = _inc(9, BASE, BASE + 600, task_id="", node_id="n1", kind="node",
                    dns="", url="", reason={"event": "offline"})
    out3 = eventview.detail(FakeStorage(incidents=[node_inc]), 9)["scope_matrix"]
    assert out3["nodes"] == [] and "没有目标任务" in out3["verdict"]["verdict"]
    # 窗口内无数据 → verdict 给说明
    out4 = eventview.detail(FakeStorage(incidents=[_inc(1, BASE, BASE + 600)],
                                        tasks=[{"id": "t1", "name": "A", "nodes": []}],
                                        nodes=[{"id": "n1", "name": "N1"}]), 1)["scope_matrix"]
    assert out4["nodes"][0]["cells"] == []
    assert "没有该目标的节点探测数据" in out4["verdict"]["verdict"]


def test_dying_block_only_for_node_events():
    hbs = [{"node_id": "n1", "ts": BASE + 240, "cpu": 91.0, "mem": 80.5},
           {"node_id": "n1", "ts": BASE - 3000, "cpu": 5.0, "mem": 30.0},    # 30 分钟窗口外
           {"node_id": "n2", "ts": BASE + 240, "cpu": 1.0, "mem": 1.0}]      # 别的节点
    node_inc = _inc(1, BASE, None, task_id="", node_id="n1", kind="node", dns="", url="",
                    reason={"event": "offline", "last_heartbeat": BASE + 300})
    store = FakeStorage(incidents=[node_inc],
                        tasks=[], nodes=[{"id": "n1", "name": "北京-1"}], heartbeats=hbs)
    out = eventview.detail(store, 1, ts=BASE + 400)
    assert store.hb_calls[0][:3] == ("n1", BASE + 300 - 1800, BASE + 300)   # 锚定 last_heartbeat
    assert out["dying"] == [{"ts": BASE + 240, "cpu": 91.0, "mem": 80.5}]
    # probe 事件：无 dying 键（契约）
    probe_store = FakeStorage(incidents=[_inc(1, BASE, BASE + 600)],
                              tasks=[{"id": "t1", "name": "A"}],
                              nodes=[{"id": "n1", "name": "N1"}], heartbeats=hbs)
    assert "dying" not in eventview.detail(probe_store, 1)
    # 无心跳数据 → 空态说明
    node_inc2 = _inc(2, BASE, None, task_id="", node_id="n1", kind="node", dns="", url="",
                     reason={"event": "offline", "last_heartbeat": BASE + 300})
    empty = eventview.detail(FakeStorage(incidents=[node_inc2]), 2, ts=BASE + 400)["dying"]
    assert empty[0]["ts"] == 0 and empty[0]["cpu"] is None and "没有该节点的心跳资源" in empty[0]["note"]
