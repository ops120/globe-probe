"""metrics 渲染单元测试：假 storage，不联网、不启服务。"""
import math
import re
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server import metrics  # noqa: E402

NOW = 1_700_000_000


# ---------------------------------------------------------------- 假 storage

def _node(**kw):
    base = {"id": "n1", "name": "win-local", "status": "online",
            "last_heartbeat": NOW - 30, "online_since": NOW - 3600,
            "cpu": 12.5, "mem": 33.33333, "tags": {}, "hb_tasks": 0,
            "local_ip": "10.0.0.1", "egress_ip": "1.2.3.4"}
    base.update(kw)
    return base


def _task(**kw):
    base = {"id": "t1", "name": "ping-223", "type": "ping", "target": "223.5.5.5",
            "enabled": 1, "avail_24h": 0.997, "streams": 2, "nodes": ["n1"],
            "dns": [], "urls": [], "interval_seconds": 30}
    base.update(kw)
    return base


class FakeStorage:
    """只实现 metrics 会用到的只读方法。"""

    def __init__(self, nodes=(), tasks=(), counts=None, config_version=7, buckets=None):
        self._nodes = [dict(n) for n in nodes]
        self._tasks = [dict(t) for t in tasks]
        self._counts = counts
        self._cver = config_version
        self._buckets = dict(buckets or {})
        self.agg_calls = []

    def stats_counts(self):
        if self._counts is not None:
            return dict(self._counts)
        return {"results": 0, "nodes": len(self._nodes),
                "tasks": len(self._tasks), "incidents_open": 0}

    def list_nodes(self):
        return [dict(n) for n in self._nodes]

    def list_tasks(self):
        return [dict(t) for t in self._tasks]

    def config_version(self):
        return self._cver

    def node_avail(self, node_id, t_from, task_ids=None):
        return {"count": 0, "ok": 0, "avail": None}

    def agg_buckets_existing(self, bucket, task_id, t_from, t_to):
        self.agg_calls.append((bucket, task_id, t_from, t_to))
        return [dict(r) for r in self._buckets.get(task_id, [])]

    def node_metrics(self, node_id, t_from, t_to, bucket):
        return []


def _counts(results=1234, nodes=1, tasks=1, incidents_open=2):
    return {"results": results, "nodes": nodes, "tasks": tasks,
            "incidents_open": incidents_open}


# ---------------------------------------------------------------- 基础指标

def test_up_and_config_version():
    text = metrics.render(FakeStorage(counts=_counts()), now_ts=NOW)
    assert "gpm_up 1" in text
    assert 'gpm_config_version{version="0.1.0"} 7' in text
    assert metrics.CONTENT_TYPE == "text/plain; version=0.0.4; charset=utf-8"


def test_summary_counts():
    s = FakeStorage(nodes=[_node(status="online"), _node(id="n2", name="bj-1", status="offline")],
                    tasks=[_task(enabled=1), _task(id="t2", name="curl-a", enabled=0)],
                    counts=_counts(results=99, nodes=2, tasks=2, incidents_open=3))
    text = metrics.render(s, now_ts=NOW)
    assert "gpm_results_total 99" in text
    assert "gpm_nodes_total 2" in text
    assert "gpm_nodes_online 1" in text
    assert "gpm_tasks_total 2" in text
    assert "gpm_tasks_enabled 1" in text
    assert "gpm_incidents_open 3" in text


def test_summary_defaults_when_counts_missing():
    s = FakeStorage(nodes=[_node()], tasks=[_task()], counts={})
    text = metrics.render(s, now_ts=NOW)
    assert "gpm_results_total 0" in text
    assert "gpm_nodes_total 1" in text
    assert "gpm_tasks_total 1" in text


# ---------------------------------------------------------------- 节点指标

def test_node_metrics_values():
    s = FakeStorage(nodes=[_node(status="online", last_heartbeat=NOW - 50,
                                 online_since=NOW - 100, cpu=12.5, mem=33.3)],
                    tasks=[])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_up{node="win-local"} 1' in text
    assert 'gpm_node_heartbeat_age_seconds{node="win-local"} 50' in text
    assert 'gpm_node_cpu_percent{node="win-local"} 12.5' in text
    assert 'gpm_node_mem_percent{node="win-local"} 33.3' in text
    assert 'gpm_node_uptime_seconds{node="win-local"} 100' in text


def test_offline_node_up_zero():
    s = FakeStorage(nodes=[_node(status="offline")], tasks=[])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_up{node="win-local"} 0' in text


def test_node_null_and_missing_fields_omitted():
    n = _node(last_heartbeat=0, online_since=0, cpu=None, mem=None, status="offline")
    s = FakeStorage(nodes=[n], tasks=[])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_up{node="win-local"} 0' in text
    assert "gpm_node_heartbeat_age_seconds" not in text
    assert "gpm_node_cpu_percent" not in text
    assert "gpm_node_mem_percent" not in text
    assert "gpm_node_uptime_seconds" not in text


def test_heartbeat_in_future_clamped_to_zero():
    s = FakeStorage(nodes=[_node(last_heartbeat=NOW + 500, online_since=NOW + 500)],
                    tasks=[])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_heartbeat_age_seconds{node="win-local"} 0' in text
    assert 'gpm_node_uptime_seconds{node="win-local"} 0' in text


def test_node_missing_name_falls_back_to_id():
    s = FakeStorage(nodes=[_node(id="id-only", name="")], tasks=[])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_up{node="id-only"} 1' in text


# ---------------------------------------------------------------- 任务指标

def test_task_metrics_values():
    s = FakeStorage(nodes=[], tasks=[_task(avail_24h=0.997, streams=2, enabled=1)])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_task_availability_24h{task="ping-223",type="ping"} 0.997' in text
    assert 'gpm_task_streams{task="ping-223",type="ping"} 2' in text
    assert 'gpm_task_enabled{task="ping-223"} 1' in text


def test_task_disabled_and_availability_none_omitted():
    s = FakeStorage(nodes=[], tasks=[_task(name="a", enabled=0, avail_24h=None)])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_task_enabled{task="a"} 0' in text
    assert 'gpm_task_streams{task="a",type="ping"} 2' in text
    assert "gpm_task_availability_24h" not in text


def test_task_availability_rounding_and_nan():
    s = FakeStorage(nodes=[], tasks=[
        _task(name="r", avail_24h=0.99668),
        _task(name="nan", avail_24h=float("nan")),
    ])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_task_availability_24h{task="r",type="ping"} 0.9967' in text
    assert 'gpm_task_availability_24h{task="nan",type="ping"} NaN' in text


def test_task_availability_fallback_from_aggregates():
    t = _task()
    t.pop("avail_24h")
    t.pop("streams")
    s = FakeStorage(nodes=[], tasks=[t],
                    buckets={"t1": [{"ts": 1, "count": 1000, "ok": 990},
                                    {"ts": 2, "count": 100, "ok": 99}]})
    text = metrics.render(s, now_ts=NOW)
    # (990+99)/(1000+100) = 0.99
    assert 'gpm_task_availability_24h{task="ping-223",type="ping"} 0.99' in text
    # streams 由 节点×DNS×URL 估算 = 1
    assert 'gpm_task_streams{task="ping-223",type="ping"} 1' in text
    assert s.agg_calls and s.agg_calls[0][0] == "1m"
    assert s.agg_calls[0][2] == NOW - 86400 and s.agg_calls[0][3] == NOW


def test_availability_fallback_empty_when_no_buckets():
    t = _task()
    t.pop("avail_24h")
    s = FakeStorage(nodes=[], tasks=[t], buckets={})
    text = metrics.render(s, now_ts=NOW)
    assert "gpm_task_availability_24h" not in text


def test_task_streams_fallback_counts_stream_matrix():
    t = _task()
    t.pop("streams")
    t["nodes"] = ["n1", "n2"]
    t["dns"] = ["8.8.8.8"]
    t["urls"] = ["https://a", "https://b"]
    s = FakeStorage(nodes=[], tasks=[t])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_task_streams{task="ping-223",type="ping"} 4' in text


# ---------------------------------------------------------------- 注释与格式

def test_help_type_and_trailing_newline():
    s = FakeStorage(nodes=[_node()], tasks=[_task()], counts=_counts())
    text = metrics.render(s, now_ts=NOW)
    helps = {ln.split()[2] for ln in text.splitlines() if ln.startswith("# HELP ")}
    types = {ln.split()[2] for ln in text.splitlines() if ln.startswith("# TYPE ")}
    assert helps == types
    for name in ("gpm_up", "gpm_config_version", "gpm_results_total", "gpm_nodes_total",
                 "gpm_nodes_online", "gpm_tasks_total", "gpm_tasks_enabled",
                 "gpm_incidents_open", "gpm_node_up", "gpm_node_heartbeat_age_seconds",
                 "gpm_node_cpu_percent", "gpm_node_mem_percent", "gpm_node_uptime_seconds",
                 "gpm_task_availability_24h", "gpm_task_streams", "gpm_task_enabled"):
        assert name in helps, name
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_label_escaping_double_quote_backslash_newline():
    weird = 'a"b\\c\nd'
    s = FakeStorage(nodes=[_node(id="n1", name=weird)], tasks=[])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_up{node="a\\"b\\\\c\\nd"} 1' in text
    # 转义后的换行不能真的断行
    for line in text.splitlines():
        if line.startswith("gpm_node_up{"):
            assert line.endswith("} 1")
            break
    else:
        raise AssertionError("gpm_node_up 行缺失")


def test_handles_sqlite3_row_and_null():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE nodes(id TEXT, name TEXT, status TEXT,"
               " last_heartbeat INTEGER, online_since INTEGER, cpu REAL, mem REAL)")
    db.execute("INSERT INTO nodes VALUES('n1','win-local','online',?,?,11.5,NULL)",
               (NOW - 30, NOW - 60))
    rows = db.execute("SELECT * FROM nodes").fetchall()

    class RowStorage(FakeStorage):
        def list_nodes(self):
            return list(rows)

    s = RowStorage(tasks=[_task()], counts=_counts())
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_node_up{node="win-local"} 1' in text
    assert 'gpm_node_cpu_percent{node="win-local"} 11.5' in text
    assert "gpm_node_mem_percent" not in text
    assert 'gpm_node_heartbeat_age_seconds{node="win-local"} 30' in text
    db.close()


# ---------------------------------------------------------------- ingest

class _CounterIngest:
    accepted_total = 10
    duplicates_total = 2
    rejected_total = 1
    truncated_total = 0
    _hidden_total = 5          # 下划线开头，忽略
    not_a_counter = "nope"     # 非数值，忽略


def test_ingest_counters_present():
    s = FakeStorage(counts=_counts())
    text = metrics.render(s, ingest=_CounterIngest(), now_ts=NOW)
    assert "gpm_ingest_accepted_total 10" in text
    assert "gpm_ingest_duplicates_total 2" in text
    assert "gpm_ingest_rejected_total 1" in text
    assert "gpm_ingest_truncated_total 0" in text
    assert "_hidden_total" not in text
    assert "not_a_counter" not in text


def test_ingest_missing_is_safe():
    s = FakeStorage(counts=_counts())
    for ingest in (None, object(), _CounterIngest.__new__(_CounterIngest)):
        text = metrics.render(s, ingest=ingest, now_ts=NOW)
        assert "gpm_up 1" in text
    assert "gpm_ingest_" not in metrics.render(s, ingest=None, now_ts=NOW)
    assert "gpm_ingest_" not in metrics.render(s, ingest=object(), now_ts=NOW)


# ---------------------------------------------------------------- 截断

def test_truncation_large_task_set():
    tasks = [_task(id=f"t{i}", name=f"ping-{i:04d}", avail_24h=0.997, streams=2)
             for i in range(3000)]
    s = FakeStorage(tasks=tasks, counts=_counts(results=0, nodes=0, tasks=3000))
    text = metrics.render(s, now_ts=NOW)

    assert len(text.encode("utf-8")) <= metrics.MAX_OUTPUT_BYTES
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "gpm_up 1" in text
    assert "# gpm: output truncated" in text
    m = re.search(r"kept \d+/\d+ nodes and (\d+)/3000 tasks", text)
    assert m, text[-200:]
    kept = int(m.group(1))
    assert 0 < kept < 3000
    assert text.count("gpm_task_enabled{") == kept
    assert text.rstrip("\n").splitlines()[-1].startswith("# gpm: output truncated")


def test_truncation_large_node_set():
    nodes = [_node(id=f"n{i}", name=f"node-{i:04d}") for i in range(4000)]
    s = FakeStorage(nodes=nodes, tasks=[], counts=_counts(results=0, nodes=4000, tasks=0))
    text = metrics.render(s, now_ts=NOW)
    assert len(text.encode("utf-8")) <= metrics.MAX_OUTPUT_BYTES
    m = re.search(r"kept (\d+)/4000 nodes", text)
    assert m and 0 < int(m.group(1)) < 4000
    assert text.count("gpm_node_up{") == int(m.group(1))


def test_no_truncation_comment_when_small():
    s = FakeStorage(nodes=[_node()], tasks=[_task()], counts=_counts())
    text = metrics.render(s, now_ts=NOW)
    assert "truncated" not in text


def test_now_ts_defaults_to_current_time():
    text = metrics.render(FakeStorage(nodes=[_node()], tasks=[], counts=_counts()),
                          now_ts=None)
    assert "gpm_up 1" in text
    m = re.search(r'gpm_node_heartbeat_age_seconds\{node="win-local"\} (\d+)', text)
    assert m and int(m.group(1)) >= 0


def test_fmt_nan_and_infinity():
    assert metrics._fmt(float("nan")) == "NaN"
    assert metrics._fmt(float("inf")) == "+Inf"
    assert metrics._fmt(float("-inf")) == "-Inf"
    assert metrics._fmt(None) is None
    assert math.isnan(float("nan"))  # 显式引用 math，避免 lint 误报


# ---------------------------------------------------------------- 值班页指标（第三期 12 / 10）

class DbStorage(FakeStorage):
    """带真实 sqlite（probe_results 表）的替身：last_success 指标走只读 SQL。"""

    def __init__(self, rows=(), **kw):
        super().__init__(**kw)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE probe_results(ts INTEGER, task_id TEXT, status TEXT)")
        self.db.executemany("INSERT INTO probe_results VALUES(?,?,?)", rows)


class ChannelStorage(FakeStorage):
    """带 list_channels / setting_get 的替身：notify_channel_up 指标。"""

    def __init__(self, channels=(), minutes="10", **kw):
        super().__init__(**kw)
        self._channels = list(channels)
        self._minutes = minutes

    def list_channels(self):
        return [dict(c) for c in self._channels]

    def setting_get(self, key, default=""):
        return self._minutes if key == "channel_selfcheck_minutes" else default


def test_task_last_success_metric_takes_most_recent_ok():
    s = DbStorage(rows=[(NOW - 100, "t1", "ok"), (NOW - 50, "t1", "fail"),
                        (NOW - 10, "t1", "ok"), (NOW - 5, "t2", "fail")],
                  tasks=[_task(), _task(id="t2", name="curl-bad", type="curl")])
    text = metrics.render(s, now_ts=NOW)
    assert ('gpm_task_last_success_timestamp_seconds'
            '{task_id="t1",name="ping-223",type="ping"} '
            f"{NOW - 10}") in text
    assert 'task_id="t2"' not in text, "无成功样本的任务不输出该系列"


def test_task_last_success_omitted_without_db_or_table():
    text = metrics.render(FakeStorage(tasks=[_task()]), now_ts=NOW)
    assert "gpm_task_last_success_timestamp_seconds" not in text

    class NoTableStorage(DbStorage):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.db.execute("DROP TABLE probe_results")

    text2 = metrics.render(NoTableStorage(tasks=[_task()]), now_ts=NOW)
    assert "gpm_task_last_success_timestamp_seconds" not in text2
    assert "gpm_up 1" in text2, "SQL 失败不影响其余指标"


def test_notify_channel_up_metric_states():
    chs = [{"id": "ch1", "name": "企微", "enabled": 1, "last_ok_at": NOW - 300},
           {"id": "ch2", "name": "钉钉", "enabled": 1, "last_ok_at": NOW - 1500},
           {"id": "ch3", "name": "停用渠道", "enabled": 0, "last_ok_at": NOW - 60},
           {"id": "ch4", "name": "从未自检", "enabled": 1, "last_ok_at": 0}]
    text = metrics.render(ChannelStorage(channels=chs), now_ts=NOW)
    assert 'gpm_notify_channel_up{channel_id="ch1",name="企微"} 1' in text
    assert 'gpm_notify_channel_up{channel_id="ch2",name="钉钉"} 0' in text
    assert 'gpm_notify_channel_up{channel_id="ch3",name="停用渠道"} 0' in text
    assert "ch4" not in text, "从未自检过的渠道不输出该系列（未知≠down）"
    # 没有 list_channels 的 storage → 整族省略
    assert "gpm_notify_channel_up" not in metrics.render(FakeStorage(), now_ts=NOW)


def test_notify_channel_up_staleness_follows_selfcheck_minutes():
    ch = [{"id": "c", "name": "n", "enabled": 1, "last_ok_at": NOW - 1500}]
    # 默认周期 10 分钟 → staleness 20 分钟：1500s 前的自检已过期
    assert 'gpm_notify_channel_up{channel_id="c",name="n"} 0' in \
        metrics.render(ChannelStorage(channels=ch, minutes="10"), now_ts=NOW)
    # 周期 30 分钟 → staleness 1 小时：1500s 内自检过 → up
    assert 'gpm_notify_channel_up{channel_id="c",name="n"} 1' in \
        metrics.render(ChannelStorage(channels=ch, minutes="30"), now_ts=NOW)


def test_notify_channel_up_label_escaping():
    s = ChannelStorage(channels=[{"id": 'c"1', "name": "a\\b", "enabled": 1,
                                  "last_ok_at": NOW - 60}])
    text = metrics.render(s, now_ts=NOW)
    assert 'gpm_notify_channel_up{channel_id="c\\"1",name="a\\\\b"} 1' in text
