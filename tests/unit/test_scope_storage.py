"""storage 新增查询单测（故障快速定位告警域）：

- task_nodes_latest_status：范围判定「各节点最近一轮」（3 节点全挂 / 1 挂 / 2 挂、
  60s 窗口外样本不计、skipped 不进结论）；
- task_last_failure / dns_answer_changes：通知【证据】与事件详情 DNS 联动的数据源；
- alert_open_keys / alert_episode_start / alert_last_escalated：升级链口径；
- alert_rules.escalate_minutes：默认 0、上限 1440 的钳制与轻量迁移。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server.diagnose import verdict  # noqa: E402
from gpm.server.storage import Storage  # noqa: E402


def make_storage(tmp_path):
    s = Storage(str(tmp_path / "scope.db"))
    t0 = 1_700_000_000
    s.create_task("t1", "curl-目标", "curl", "https://example.com", ["https://example.com"],
                  {}, [], 30, t0)
    s.create_task("t-dns", "dns-目标", "dns", "example.com", [], {}, [], 60, t0)
    for name in ("北京", "上海", "广州"):
        s.register_node(name, "h", {}, "v1", {}, t0)
    return s, t0


def _row(s, ts, node_name, status, error_class="", metrics=None, task_id="t1"):
    nid = next(n["id"] for n in s.list_nodes() if n["name"] == node_name)
    return {"ts": ts, "task_id": task_id, "node_id": nid, "type": "curl", "status": status,
            "error_class": error_class, "error": "" if status == "ok" else "boom",
            "metrics": metrics or {}}


def _states_of(s, ts, **kw):
    rows = s.task_nodes_latest_status("t1", ts, **kw)
    return verdict([{"node_name": r.get("node_name") or r.get("node_id"), "status": r["status"]}
                    for r in rows]), rows


# ---------------- 范围判定：三档 ----------------

def test_scope_all_nodes_failed(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    s.insert_results([_row(s, ts, "北京", "fail", "timeout"),
                      _row(s, ts, "上海", "fail", "timeout"),
                      _row(s, ts, "广州", "fail", "timeout")], ts)
    v, rows = _states_of(s, ts + 30)
    assert v["mode"] == "all_nodes" and v["failed"] == 3 and v["total"] == 3
    assert v["verdict"].startswith("全节点失败")
    assert {r["node_name"] for r in rows} == {"北京", "上海", "广州"}
    assert all(r["error_class"] == "timeout" for r in rows)


def test_scope_single_node_failed(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    s.insert_results([_row(s, ts, "北京", "fail", "refused"),
                      _row(s, ts, "上海", "ok"),
                      _row(s, ts, "广州", "ok")], ts)
    v, _ = _states_of(s, ts + 30)
    assert v["mode"] == "single_node" and v["failed"] == 1 and v["total"] == 3
    assert v["failed_names"] == ["北京"]


def test_scope_partial_failed(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    s.insert_results([_row(s, ts, "北京", "fail", "timeout"),
                      _row(s, ts, "上海", "fail", "timeout"),
                      _row(s, ts, "广州", "ok")], ts)
    v, _ = _states_of(s, ts + 30)
    assert v["mode"] == "partial" and v["failed"] == 2 and v["total"] == 3


def test_scope_window_and_latest_per_node(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    # 每节点两轮：旧轮 fail，最近一轮 ok → 以最近一轮为准
    s.insert_results([_row(s, ts - 60, "北京", "fail", "timeout"),
                      _row(s, ts, "北京", "ok"),
                      _row(s, ts, "上海", "ok")], ts)
    v, rows = _states_of(s, ts + 30)
    assert v["mode"] == "" and v["failed"] == 0            # 最近一轮全 ok
    bj = next(r for r in rows if r["node_name"] == "北京")
    assert bj["status"] == "ok" and bj["ts"] == ts
    # 超出 60s 窗口的样本不算「最近一轮」→ 空结果
    assert s.task_nodes_latest_status("t1", ts + 600) == []
    # skipped 不计入分母（但查询如实返回 skipped 行，由 verdict 过滤）
    s.insert_results([_row(s, ts + 60, "北京", "skipped"),
                      _row(s, ts + 60, "上海", "fail", "timeout")], ts + 60)
    v2, rows2 = _states_of(s, ts + 90)
    by = {r["node_name"]: r["status"] for r in rows2}
    assert by == {"北京": "skipped", "上海": "fail"}
    assert v2["mode"] == "partial" and v2["total"] == 1 and v2["failed"] == 1


def test_scope_window_clamped_to_60s(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    s.insert_results([_row(s, ts, "北京", "fail", "timeout")], ts)
    # 传更大的窗口也会被钳到 ≤60s：90s 前的样本已过期
    assert s.task_nodes_latest_status("t1", ts + 90, window_seconds=600) == []
    assert s.task_nodes_latest_status("t1", ts + 30, window_seconds=600)[0]["status"] == "fail"


# ---------------- 证据 / DNS 变更 ----------------

def test_task_last_failure(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    s.insert_results([_row(s, ts - 30, "北京", "fail", "timeout"),
                      _row(s, ts - 20, "上海", "ok"),
                      _row(s, ts, "广州", "fail", "refused",
                           metrics={"total_time": 5000.0, "ttfb": 120.0})], ts)
    ev = s.task_last_failure("t1", ts + 10)
    assert ev["node_id"] == next(n["id"] for n in s.list_nodes() if n["name"] == "广州")
    assert ev["error_class"] == "refused" and ev["metrics"]["total_time"] == 5000.0
    assert s.task_last_failure("t1", ts - 40) is None      # 早于最早失败 → 无
    assert s.task_last_failure("t-none", ts + 10) is None


def test_dns_answer_changes(tmp_path):
    s, t0 = make_storage(tmp_path)
    ts = t0 + 120
    nid = next(n["id"] for n in s.list_nodes() if n["name"] == "北京")
    rows = [
        {"ts": ts - 60, "task_id": "t-dns", "node_id": nid, "type": "dns", "status": "ok",
         "metrics": {"answers": ["1.1.1.1"], "changed": True, "prev_answers": ["2.2.2.2"]}},
        {"ts": ts, "task_id": "t-dns", "node_id": nid, "type": "dns", "status": "ok",
         "metrics": {"answers": ["1.1.1.1"], "changed": False}},
        {"ts": ts, "task_id": "t1", "node_id": nid, "type": "curl", "status": "ok",
         "metrics": {}},
    ]
    s.insert_results(rows, ts)
    changes = s.dns_answer_changes("example.com", ts - 86400, ts + 60)
    assert len(changes) == 1 and changes[0]["ts"] == ts - 60
    assert changes[0]["answers"] == ["1.1.1.1"] and changes[0]["prev_answers"] == ["2.2.2.2"]
    assert changes[0]["task_id"] == "t-dns"
    assert s.dns_answer_changes("other.com", ts - 86400, ts + 60) == []
    assert s.dns_answer_changes("", ts - 86400, ts + 60) == []


# ---------------- 升级链辅助查询 ----------------

def _rule(s, rid="r1", **kw):
    fields = {"name": "rule-" + rid, "metric": "avail", "op": "lt", "threshold": 0.9,
              "channel_ids": [], "task_id": "t1", "silence_seconds": 0}
    fields.update(kw)
    return s.create_rule(rid, fields, 1_700_000_000)


def test_alert_episode_and_escalation_state(tmp_path):
    s, t0 = make_storage(tmp_path)
    rule = _rule(s)
    # 第一轮 firing（episode 起点 t0+60）
    s.alert_add(t0 + 60, rule, "t1", "firing", "a", "b", {}, True, 1, 1)
    assert s.alert_open_keys(rule["id"]) == ["t1"]
    assert s.alert_episode_start(rule["id"], "t1") == t0 + 60
    assert s.alert_last_escalated(rule["id"], "t1") == 0
    # remind / 升级行不改变 episode 起点
    s.alert_add(t0 + 120, rule, "t1", "firing", "a", "b", {}, True, 1, 1)
    s.alert_add(t0 + 180, rule, "t1", "firing", "【升级】a", "b", {}, True, 1, 1,
                detail=f"escalated_at={t0 + 180}")
    assert s.alert_episode_start(rule["id"], "t1") == t0 + 60
    assert s.alert_last_escalated(rule["id"], "t1") == t0 + 180
    # 恢复 → 无未恢复告警
    s.alert_add(t0 + 300, rule, "t1", "resolved", "a", "b", {}, True, 1, 1)
    assert s.alert_open_keys(rule["id"]) == []
    assert s.alert_episode_start(rule["id"], "t1") == 0
    # 重新 firing → 新 episode 起点，但升级时间仍可追溯
    s.alert_add(t0 + 600, rule, "t1", "firing", "a", "b", {}, True, 1, 1)
    assert s.alert_episode_start(rule["id"], "t1") == t0 + 600
    assert s.alert_last_escalated(rule["id"], "t1") == t0 + 180
    # 不同 key 互不影响
    assert s.alert_episode_start(rule["id"], "t2") == 0


def test_escalate_minutes_clamped_and_migrated(tmp_path):
    s, t0 = make_storage(tmp_path)
    rule = _rule(s, "r0")                       # 未配置 → 默认 0（关闭）
    assert rule["escalate_minutes"] == 0
    assert _rule(s, "r1", escalate_minutes=30)["escalate_minutes"] == 30
    assert _rule(s, "r2", escalate_minutes=99999)["escalate_minutes"] == 1440
    assert _rule(s, "r3", escalate_minutes=-5)["escalate_minutes"] == 0
    assert _rule(s, "r4", escalate_minutes="abc")["escalate_minutes"] == 0
    s.update_rule("r1", {"escalate_minutes": 120}, t0)
    assert s.list_rules()[0]["escalate_minutes"] == 120 or any(
        r["id"] == "r1" and r["escalate_minutes"] == 120 for r in s.list_rules())
