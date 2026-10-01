"""tests/unit/test_report.py —— SLA 报表单测（假 storage，不联网、不碰真库）。

守护三条硬规则：

1. 数值只走聚合桶（bucket ∈ {1m,5m,1h,1d}），不读 probe_results 原始表；
2. avail 是 0~1 的小数，无数据必须是 None 而不是 0；
3. 节点离线（kind='node'）不计入目标停机：downtime/mttr/mtbf 只算 kind='probe'。
"""
from __future__ import annotations

import pytest

from gpm.server.report import BUCKETS, DAY, daily_series, digest_text, sla

BASE = 1767225600  # 2026-01-01 00:00:00 UTC（正好落在 UTC 日界）

ALLOWED_METHODS = {"list_tasks", "list_nodes", "list_incidents", "node_avail",
                   "agg_buckets_existing", "result_streams", "get_task"}


# ---------------------------------------------------------------- 假 storage

class FakeStorage:
    """按 report.py 允许清单实现的假 storage：记录调用参数并返回构造数据。"""

    def __init__(self, tasks=None, nodes=None, incidents=None, buckets=None,
                 streams=None, node_avail=None):
        self.tasks = tasks or []
        self.nodes = nodes or []
        self.incidents = incidents or []
        self.buckets = buckets or {}       # (bucket, task_id) -> [row, ...]
        self.streams = streams or {}       # task_id -> [stream, ...]
        self.node_avail_map = node_avail or {}
        self.calls = []                    # [(method, *args), ...]
        self.buckets_seen = []             # 所有传出的 bucket，供红线断言
        self.incident_limit = None
        self.node_avail_calls = []

    # --- 允许清单内的接口 ---
    def list_tasks(self):
        self.calls.append(("list_tasks",))
        return [dict(t) for t in self.tasks]

    def list_nodes(self):
        self.calls.append(("list_nodes",))
        return [dict(n) for n in self.nodes]

    def list_incidents(self, limit=30):
        self.calls.append(("list_incidents", limit))
        self.incident_limit = limit
        rows = sorted(self.incidents, key=lambda i: i.get("started_at") or 0, reverse=True)
        return [dict(i) for i in rows[:limit]]

    def node_avail(self, node_id, t_from, task_ids=None):
        self.calls.append(("node_avail", node_id, t_from, task_ids))
        self.node_avail_calls.append((node_id, t_from, task_ids))
        return dict(self.node_avail_map.get(node_id, {"count": 0, "ok": 0, "avail": None}))

    def agg_buckets_existing(self, bucket, task_id, t_from, t_to):
        self.calls.append(("agg_buckets_existing", bucket, task_id, t_from, t_to))
        self.buckets_seen.append(bucket)
        rows = self.buckets.get((bucket, task_id), [])
        return [dict(r) for r in rows if t_from <= r["ts"] <= t_to]

    def agg_read(self, bucket, task_id, node_id, dns, url, t_from, t_to):
        self.calls.append(("agg_read", bucket, task_id, node_id, dns, url, t_from, t_to))
        self.buckets_seen.append(bucket)
        return []

    def result_streams(self, task_id):
        self.calls.append(("result_streams", task_id))
        return [dict(s) for s in self.streams.get(task_id, [])]

    def get_task(self, tid):
        self.calls.append(("get_task", tid))
        for t in self.tasks:
            if t["id"] == tid:
                return dict(t)
        return None

    # --- 断言辅助 ---
    def method_names(self):
        return {c[0] for c in self.calls}


def agg_row(ts, count, ok, rtt=None, p95=None, loss=None):
    """一行聚合数据（对齐 agg_buckets_existing 的返回字段）。"""
    return {"ts": ts, "count": count, "ok": ok, "fail": count - ok,
            "rtt_avg": rtt, "rtt_p95": p95,
            "avail_rate": (ok / count) if count else None, "loss_rate": loss}


# ---------------------------------------------------------------- overall/tasks/nodes

def test_sla_overall_tasks_nodes_counts():
    t_from, t_to = BASE, BASE + 6 * 3600
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "首页", "type": "curl"},
               {"id": "t2", "name": "DNS", "type": "ping"}],
        nodes=[{"id": "n1", "name": "北京", "status": "online", "online_since": t_from - 3600},
               {"id": "n2", "name": "上海", "status": "offline", "online_since": 0}],
        buckets={
            ("1h", "t1"): [agg_row(t_from, 100, 99, 10.0, 20.0, 0.01),
                           agg_row(t_from + 3600, 100, 100, 12.0, 25.0, 0.0)],
            ("1h", "t2"): [agg_row(t_from, 50, 40, 5.0, 8.0, 0.2)],
        },
        streams={"t1": [{}, {}], "t2": [{}]},
        node_avail={"n1": {"count": 150, "ok": 139, "avail": 139 / 150},
                    "n2": {"count": 0, "ok": 0, "avail": None}},
    )
    rep = sla(st, t_from, t_to)

    assert rep["window"] == {"from": t_from, "to": t_to, "hours": 6.0}
    assert rep["filter"] == {"task_id": "", "node_id": ""}

    ov = rep["overall"]
    assert (ov["count"], ov["ok"], ov["fail"]) == (250, 239, 11)
    assert ov["avail"] == pytest.approx(round(239 / 250, 4))
    # rtt/loss 按 count 加权：(10*100 + 12*100 + 5*50) / 250
    assert ov["rtt_avg"] == pytest.approx(9.8)
    assert ov["rtt_p95"] == 25.0
    assert ov["loss_rate"] == pytest.approx(0.044)

    tasks = {t["task_id"]: t for t in rep["tasks"]}
    assert set(tasks) == {"t1", "t2"}
    assert (tasks["t1"]["count"], tasks["t1"]["ok"], tasks["t1"]["fail"]) == (200, 199, 1)
    assert tasks["t1"]["avail"] == pytest.approx(0.995)
    assert tasks["t1"]["streams"] == 2
    assert tasks["t1"]["name"] == "首页" and tasks["t1"]["type"] == "curl"
    assert tasks["t2"]["avail"] == pytest.approx(0.8)

    nodes = {n["node_id"]: n for n in rep["nodes"]}
    assert (nodes["n1"]["count"], nodes["n1"]["ok"], nodes["n1"]["fail"]) == (150, 139, 11)
    assert nodes["n1"]["avail"] == pytest.approx(round(139 / 150, 4))
    assert nodes["n1"]["uptime_seconds"] == t_to - (t_from - 3600)
    assert nodes["n2"]["status"] == "offline"
    assert nodes["n2"]["uptime_seconds"] == 0

    assert set(st.buckets_seen) <= set(BUCKETS)
    assert st.method_names() <= ALLOWED_METHODS


def test_sla_no_data_gives_none_not_zero():
    t_from, t_to = BASE, BASE + 3600
    st = FakeStorage(tasks=[{"id": "t1", "name": "静默任务", "type": "ping"}],
                     nodes=[{"id": "n1", "name": "空节点", "status": "online",
                             "online_since": 0}])
    rep = sla(st, t_from, t_to)
    ov = rep["overall"]
    assert (ov["count"], ov["ok"], ov["fail"]) == (0, 0, 0)
    assert ov["avail"] is None and ov["rtt_avg"] is None
    assert ov["rtt_p95"] is None and ov["loss_rate"] is None
    # 无数据对象仍在列表里，但 avail 必须是 None
    assert rep["tasks"][0]["count"] == 0
    assert rep["tasks"][0]["avail"] is None
    assert rep["tasks"][0]["streams"] == 0
    assert rep["nodes"][0]["count"] == 0
    assert rep["nodes"][0]["avail"] is None


def test_sla_task_and_node_filters():
    t_from, t_to = BASE, BASE + 3600
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "首页", "type": "curl"},
               {"id": "t2", "name": "DNS", "type": "ping"}],
        nodes=[{"id": "n1", "name": "北京", "status": "online", "online_since": t_from},
               {"id": "n2", "name": "上海", "status": "online", "online_since": t_from}],
        buckets={("1h", "t2"): [agg_row(t_from, 10, 9)]},
        node_avail={"n2": {"count": 10, "ok": 9, "avail": 0.9}},
    )
    rep = sla(st, t_from, t_to, task_id="t2", node_id="n2")
    assert rep["filter"] == {"task_id": "t2", "node_id": "n2"}
    assert [t["task_id"] for t in rep["tasks"]] == ["t2"]
    assert [n["node_id"] for n in rep["nodes"]] == ["n2"]
    assert rep["overall"]["count"] == 10
    # 节点可用率要按过滤后的任务集合查
    assert st.node_avail_calls == [("n2", t_from, ["t2"])]
    agg_calls = [c for c in st.calls if c[0] == "agg_buckets_existing"]
    assert len(agg_calls) == 1, "指定 task_id 时不应再查其它任务"


def test_sla_unknown_task_filter_still_returns_row():
    st = FakeStorage(tasks=[{"id": "t1", "name": "首页", "type": "curl"}])
    rep = sla(st, BASE, BASE + 3600, task_id="ghost")
    assert [t["task_id"] for t in rep["tasks"]] == ["ghost"]
    assert rep["tasks"][0]["avail"] is None
    assert rep["tasks"][0]["count"] == 0


def test_sla_reversed_window_is_normalized():
    st = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"}])
    rep = sla(st, BASE + 3600, BASE)
    assert rep["window"] == {"from": BASE, "to": BASE + 3600, "hours": 1.0}


# ---------------------------------------------------------------- 事件统计

def test_sla_incidents_probe_only_downtime_mttr_mtbf():
    t_from, t_to = BASE, BASE + 6 * 3600
    started = t_from + 300
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "首页", "type": "curl"}],
        nodes=[{"id": "n1", "name": "北京", "status": "online", "online_since": t_from}],
        incidents=[
            {"id": 1, "task_id": "t1", "node_id": "n1", "dns": "8.8.8.8", "url": "",
             "started_at": started, "ended_at": started + 600, "duration_ms": 600000,
             "kind": "probe", "reason": {"error_class": "timeout"}},
            {"id": 2, "task_id": "t1", "node_id": "n1", "dns": "", "url": "",
             "started_at": t_to - 120, "ended_at": None, "duration_ms": None,
             "kind": "probe", "reason": {}},
            # 节点侧事件：6 小时离线，绝不能进 downtime/mttr/mtbf
            {"id": 3, "task_id": "", "node_id": "n1", "dns": "", "url": "",
             "started_at": t_from, "ended_at": t_to, "duration_ms": 6 * 3600 * 1000,
             "kind": "node", "reason": {"event": "offline"}},
            # 与窗口无交集的旧事件：必须被剔除
            {"id": 4, "task_id": "t1", "node_id": "n1", "dns": "", "url": "",
             "started_at": t_from - 10000, "ended_at": t_from - 9000,
             "duration_ms": 1000000, "kind": "probe", "reason": {}},
        ],
    )
    inc = sla(st, t_from, t_to)["incidents"]

    assert inc["total"] == 3 and inc["open"] == 1
    assert inc["downtime_seconds"] == 720, "600（已恢复）+ 120（进行中算到窗口末尾）"
    assert inc["downtime_seconds"] != 6 * 3600, "节点离线不得计入目标停机"
    assert inc["mttr_seconds"] == 600.0, "只对已恢复的探测类事件取平均"
    assert inc["mtbf_seconds"] == pytest.approx(6 * 3600 / 2)
    assert st.incident_limit and st.incident_limit > 0

    by_id = {i["id"]: i for i in inc["items"]}
    assert set(by_id) == {1, 2, 3}
    assert [i["started_at"] for i in inc["items"]] == sorted(
        [i["started_at"] for i in inc["items"]], reverse=True)
    assert by_id[2]["duration_ms"] == 120000
    assert by_id[2]["ended_at"] is None
    assert by_id[2]["title"] == "首页 · 默认线路"
    assert by_id[1]["title"] == "首页 · 8.8.8.8"
    assert by_id[1]["task_name"] == "首页" and by_id[1]["node_name"] == "北京"
    assert by_id[3]["kind"] == "node" and by_id[3]["title"].startswith("节点")


def test_sla_incidents_empty_mttr_mtbf_none():
    st = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"}], incidents=[])
    inc = sla(st, BASE, BASE + 3600)["incidents"]
    assert inc["total"] == 0 and inc["open"] == 0
    assert inc["downtime_seconds"] == 0
    assert inc["mttr_seconds"] is None
    assert inc["mtbf_seconds"] is None
    assert inc["items"] == []


def test_sla_node_only_incidents_do_not_create_target_metrics():
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "a", "type": "ping"}],
        nodes=[{"id": "n1", "name": "北京", "status": "offline", "online_since": 0}],
        incidents=[{"id": 9, "task_id": "", "node_id": "n1", "dns": "", "url": "",
                    "started_at": BASE + 60, "ended_at": None, "duration_ms": None,
                    "kind": "node", "reason": {}}],
    )
    inc = sla(st, BASE, BASE + 3600)["incidents"]
    assert inc["total"] == 1 and inc["open"] == 1
    assert inc["downtime_seconds"] == 0
    assert inc["mttr_seconds"] is None and inc["mtbf_seconds"] is None
    assert inc["items"][0]["title"] == "节点离线 · 北京"


# ---------------------------------------------------------------- bucket 红线

def test_bucket_selection_1h_short_1d_long():
    st = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"}])
    sla(st, BASE, BASE + 2 * DAY)                      # 恰好 2 天 → 1h
    agg = [c for c in st.calls if c[0] == "agg_buckets_existing"]
    assert agg and set(c[1] for c in agg) == {"1h"}

    st2 = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"}],
                      buckets={("1d", "t1"): [agg_row(BASE, 10, 9)]})
    sla(st2, BASE, BASE + 3 * DAY)                     # > 2 天 → 1d
    assert st2.buckets_seen == ["1d"]


def test_1d_missing_falls_back_to_1h():
    st = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"}],
                     buckets={("1h", "t1"): [agg_row(BASE, 20, 20, 8.0, 9.0, 0.0)]})
    ov = sla(st, BASE, BASE + 5 * DAY)["overall"]
    assert st.buckets_seen == ["1d", "1h"]
    assert (ov["count"], ov["ok"], ov["avail"]) == (20, 20, 1.0)


def test_every_bucket_is_whitelisted_and_no_raw_table():
    for span in (600, 3600, DAY, 2 * DAY, 3 * DAY, 30 * DAY):
        t_to = BASE + span
        st = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"},
                                {"id": "t2", "name": "b", "type": "curl"}],
                         nodes=[{"id": "n1", "name": "n", "status": "online",
                                 "online_since": BASE}],
                         incidents=[])
        sla(st, BASE, t_to)
        sla(st, BASE, t_to, task_id="t1", node_id="n1")
        daily_series(st, "t1", 3, t_to)
        daily_series(st, "", 2, t_to)
        digest_text(st, 6, t_to)

        assert st.buckets_seen, "应当有聚合桶查询"
        assert set(st.buckets_seen) <= set(BUCKETS), st.buckets_seen
        assert st.method_names() <= ALLOWED_METHODS, st.method_names()
    assert set(BUCKETS) == {"1m", "5m", "1h", "1d"}


# ---------------------------------------------------------------- daily_series

def test_daily_series_days_ascending_and_missing_day():
    t_now = BASE + 6 * 3600
    d1, d2, d3 = BASE - 2 * DAY, BASE - DAY, BASE
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "首页", "type": "curl"}],
        buckets={("1d", "t1"): [agg_row(d1, 100, 99, 10.0, 20.0, 0.01),
                                agg_row(d3, 40, 40, 12.0, 25.0, 0.0)]},
    )
    out = daily_series(st, "t1", 3, t_now)

    assert [r["day"] for r in out] == ["2025-12-30", "2025-12-31", "2026-01-01"]
    assert [r["ts"] for r in out] == [d1, d2, d3]
    assert (out[0]["count"], out[0]["ok"], out[0]["fail"]) == (100, 99, 1)
    assert out[0]["avail"] == pytest.approx(0.99)
    assert out[0]["rtt_avg"] == 10.0
    # 缺数据的日期也要有行：count=0 / avail=None
    assert out[1]["count"] == 0 and out[1]["ok"] == 0
    assert out[1]["avail"] is None and out[1]["rtt_avg"] is None
    assert out[2]["count"] == 40 and out[2]["avail"] == 1.0
    assert set(st.buckets_seen) <= set(BUCKETS)


def test_daily_series_falls_back_to_1h():
    t_now = BASE + 6 * 3600
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "首页", "type": "curl"}],
        buckets={("1h", "t1"): [agg_row(BASE - DAY + 3600, 10, 9, 4.0, 5.0, 0.1),
                                agg_row(BASE + 3600, 8, 8, 6.0, 6.0, 0.0)]},
    )
    out = daily_series(st, "t1", 2, t_now)
    assert [r["day"] for r in out] == ["2025-12-31", "2026-01-01"]
    assert st.buckets_seen == ["1d", "1h"]
    assert out[0]["count"] == 10 and out[0]["avail"] == pytest.approx(0.9)
    assert out[1]["count"] == 8 and out[1]["avail"] == 1.0
    assert out[1]["rtt_avg"] == 6.0


def test_daily_series_tops_up_current_day_with_1h():
    t_now = BASE + 6 * 3600
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "首页", "type": "curl"}],
        buckets={("1d", "t1"): [agg_row(BASE - DAY, 10, 10, 4.0, 5.0, 0.0)],
                 ("1h", "t1"): [agg_row(BASE + 3600, 6, 5, 9.0, 11.0, 0.5)]},
    )
    out = daily_series(st, "t1", 2, t_now)
    assert st.buckets_seen == ["1d", "1h"], "日桶缺当天时用 1h 补齐"
    assert out[0]["count"] == 10
    assert out[1]["count"] == 6
    assert out[1]["avail"] == pytest.approx(round(5 / 6, 4))
    assert out[1]["rtt_avg"] == 9.0


def test_daily_series_without_task_id_sums_all_tasks():
    st = FakeStorage(
        tasks=[{"id": "t1", "name": "a", "type": "ping"},
               {"id": "t2", "name": "b", "type": "ping"}],
        buckets={("1d", "t1"): [agg_row(BASE - DAY, 10, 9, 1.0, 2.0, 0.0)],
                 ("1d", "t2"): [agg_row(BASE - DAY, 5, 5, 3.0, 4.0, 0.0)]},
    )
    out = daily_series(st, "", 2, BASE)
    assert [r["day"] for r in out] == ["2025-12-31", "2026-01-01"]
    assert out[0]["count"] == 15 and out[0]["ok"] == 14
    assert out[0]["avail"] == pytest.approx(round(14 / 15, 4))
    assert out[0]["rtt_avg"] == pytest.approx(round((10 * 1.0 + 5 * 3.0) / 15, 2))
    assert out[1]["count"] == 0 and out[1]["avail"] is None


def test_daily_series_days_floor_is_one():
    st = FakeStorage(tasks=[{"id": "t1", "name": "a", "type": "ping"}])
    out = daily_series(st, "t1", 0, BASE)
    assert len(out) == 1 and out[0]["day"] == "2026-01-01"


# ---------------------------------------------------------------- digest_text

def _digest_fixture(hours=6):
    t_now = BASE + hours * 3600
    tasks, buckets = [], {}
    for i, (name, av) in enumerate([("差任务", 0.80), ("中任务", 0.95),
                                    ("好任务", 0.999), ("最佳任务", 1.0)]):
        tid = "t%d" % i
        tasks.append({"id": tid, "name": name, "type": "ping"})
        buckets[("1h", tid)] = [agg_row(BASE + h * 3600, 100, int(av * 100),
                                        10.0 + i, 20.0 + i, 0.1)
                                for h in range(hours)]
    st = FakeStorage(
        tasks=tasks,
        buckets=buckets,
        nodes=[{"id": "n1", "name": "北京", "status": "online", "online_since": BASE},
               {"id": "n2", "name": "上海", "status": "offline", "online_since": 0}],
        incidents=[{"id": 1, "task_id": "t0", "node_id": "n1", "dns": "", "url": "",
                    "started_at": BASE, "ended_at": BASE + 1800, "duration_ms": 1800000,
                    "kind": "probe", "reason": {"error_class": "timeout"}}],
    )
    return st, t_now


def test_digest_text_title_and_sections():
    st, t_now = _digest_fixture(6)
    title, body = digest_text(st, 6, t_now)

    assert title == "gpm 巡检报告 · 最近 6 小时"
    # 整体可用率与探测总数
    assert "可用率" in body
    assert "探测 2400 次" in body
    # 最差 3 个任务（最佳的那个不能出现）
    assert "差任务" in body and "中任务" in body and "好任务" in body
    assert "最佳任务" not in body
    assert "80.00%" in body
    # 事件（数量 + 最长 3 条）
    assert "事件" in body
    assert "新增/进行中 1 条" in body
    assert "时长最长的 3 条" in body
    assert "差任务 · 默认线路" in body and "已恢复" in body
    # 节点在线/离线
    assert "节点" in body
    assert "在线 1 个 / 离线 1 个" in body
    assert "上海" in body
    assert set(st.buckets_seen) <= set(BUCKETS)


def test_digest_text_hours_in_title():
    st, _ = _digest_fixture(24)
    title, body = digest_text(st, 24, BASE + 24 * 3600)
    assert title == "gpm 巡检报告 · 最近 24 小时"
    assert "### 节点" in body


def test_digest_text_no_data_uses_dash_not_zero():
    st = FakeStorage(tasks=[], nodes=[], incidents=[])
    title, body = digest_text(st, 6, BASE + 6 * 3600)
    assert title == "gpm 巡检报告 · 最近 6 小时"
    assert "可用率" in body and "事件" in body and "节点" in body
    assert "可用率：—" in body
    assert "0.00%" not in body, "无数据不能渲染成 0%"
    assert "窗口内没有任务探测数据。" in body
    assert "窗口内没有新增事件。" in body
    assert "在线 0 个 / 离线 0 个" in body
