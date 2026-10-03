"""事件生命周期回归：收口必须可信，不能留下僵尸事件。

每一条都先在线上实例上复现过（.docs/ONCALL_OPTIMIZATION_2.md 根因 1.1 / 1.2 / 1.3）：
win-local 节点在线却挂着「离线 14.4 小时」、curl-baidu-multi 最后 3 条样本连续是 ok 却
仍开着、ping-223 停用后事件挂 47 小时。这里把那些现场固化成用例。

覆盖：
- 内存状态丢失（服务重启）后，ok 结果仍要能收口 —— 以库为准而不是只认内存
- rebuild_stream 真的重建状态（原实现被自己覆盖成空），rebuild_all 接上启动路径
- 停用任务 / 删除任务立即收口
- 陈旧事件按「最后一条样本」自动收口，且不动原始 reason
- 节点走重新注册恢复也要收口（原先 register 置 online 使 node_touch 的关闭分支失效）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server.incidents import IncidentMachine  # noqa: E402
from gpm.server.storage import Storage  # noqa: E402

T0 = int(time.time())


def _store(tmp_path, name="inc.db"):
    return Storage(str(tmp_path / name))


def _machine(s, **kw):
    return IncidentMachine(s, fail_threshold=3, recover_threshold=2, flap_window=0, **kw)


def _inc(s, inc_id):
    return s.db.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()


def _open(s, m, task="t1", node="n1", dns="", url="", ts=T0):
    """喂 3 次 fail，产生一条未恢复事件，返回 incident id。ts 可回拨以模拟历史事件。"""
    for i in range(3):
        m.on_result(task, node, dns, url, "fail", ts + i * 10, "timeout")
    row = s.db.execute(
        "SELECT id FROM incidents WHERE task_id=? AND node_id=? AND ended_at IS NULL",
        (task, node)).fetchone()
    assert row, "未产生事件"
    return int(row["id"])


def _put(s, task, node, status, ts, error_class="timeout", dns="", url="", typ="ping"):
    s.db.execute(
        "INSERT OR REPLACE INTO probe_results(ts,task_id,node_id,type,dns,url,status,"
        "error_class,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (ts, task, node, typ, dns, url, status, error_class, ts))
    s.db.commit()


# --------------------------------------------------------------- 根因 1.1

def test_recovery_closes_incident_even_after_state_loss(tmp_path):
    """服务重启（内存状态丢失）后恢复，仍必须收口——线上僵尸事件的主因。"""
    s = _store(tmp_path)
    m1 = _machine(s)
    iid = _open(s, m1)

    m2 = _machine(s)                       # 全新实例 = 重启，内存里没有任何 state
    m2.on_result("t1", "n1", "", "", "ok", T0 + 100, "")
    m2.on_result("t1", "n1", "", "", "ok", T0 + 110, "")
    assert _inc(s, iid)["ended_at"] is not None, "重启后恢复没收口 → 僵尸事件"


def test_repeated_ok_does_not_requery_db_every_time(tmp_path):
    """no_open 缓存：确认库里没有未恢复事件后，后续 ok 不再回查（避免每条样本一次查询）。"""
    s = _store(tmp_path)
    calls = {"n": 0}
    real = s.open_incident_for

    def counting(*a):
        calls["n"] += 1
        return real(*a)
    s.open_incident_for = counting          # type: ignore[method-assign]

    m = _machine(s)
    for i in range(20):
        m.on_result("t1", "n1", "", "", "ok", T0 + i, "")
    assert calls["n"] <= 2, "首次确认后不应再回查库，实际 %d 次" % calls["n"]


def test_rebuild_stream_keeps_replayed_state(tmp_path):
    """原实现用空状态覆盖重放结果 —— 重建等于没做。"""
    s = _store(tmp_path)
    for i in range(3):
        _put(s, "t1", "n1", "fail", T0 + i * 10)
    m1 = _machine(s)
    iid = _open(s, m1)

    m2 = _machine(s)
    m2.rebuild_stream("t1", "n1", "", "")
    assert m2.state[("t1", "n1", "", "")]["incident_id"] == iid


def test_rebuild_all_adopts_streams_with_open_incidents(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        _put(s, "t1", "n1", "fail", T0 + i * 10)
    m1 = _machine(s)
    iid = _open(s, m1)

    m2 = _machine(s)
    assert m2.rebuild_all() == 1
    assert m2.state[("t1", "n1", "", "")]["incident_id"] == iid


def test_rebuild_does_not_create_new_incidents(tmp_path):
    """重放历史不得凭空新建事件：那条事件早该在首次失败时就落库了。"""
    s = _store(tmp_path)
    for i in range(3):
        _put(s, "t1", "n1", "fail", T0 + i * 10)
    m = _machine(s)
    m.rebuild_stream("t1", "n1", "", "")
    n = s.db.execute("SELECT COUNT(*) c FROM incidents").fetchone()["c"]
    assert n == 0


# --------------------------------------------------------------- 停用 / 删除

def test_disabling_task_closes_open_incidents(tmp_path):
    s = _store(tmp_path)
    s.create_task("t1", "pinger", "ping", "1.1.1.1", [], {}, [], 60, T0)
    iid = _open(s, _machine(s))
    s.update_task("t1", {"enabled": False}, T0 + 50)
    row = _inc(s, iid)
    assert row["ended_at"] is not None
    assert "停用" in (row["note"] or "")


def test_reenabling_task_does_not_touch_closed_incidents(tmp_path):
    s = _store(tmp_path)
    s.create_task("t1", "pinger", "ping", "1.1.1.1", [], {}, [], 60, T0)
    iid = _open(s, _machine(s))
    s.update_task("t1", {"enabled": False}, T0 + 50)
    s.update_task("t1", {"enabled": True}, T0 + 60)
    assert _inc(s, iid)["ended_at"] is not None


def test_deleting_task_closes_open_incidents(tmp_path):
    s = _store(tmp_path)
    s.create_task("t1", "pinger", "ping", "1.1.1.1", [], {}, [], 60, T0)
    iid = _open(s, _machine(s))
    s.delete_task("t1", T0 + 50)
    row = _inc(s, iid)
    assert row["ended_at"] is not None and "删除" in (row["note"] or "")


# --------------------------------------------------------------- 陈旧（沉默 ≠ 故障）

def test_stale_incident_closed_at_last_sample_time(tmp_path):
    s = _store(tmp_path)
    started = T0 - 40 * 3600                   # 事件产生于 40 小时前
    iid = _open(s, _machine(s), ts=started)
    last = started + 30                        # 最后一次样本也在那时
    _put(s, "t1", "n1", "fail", last)
    closed = s.close_stale_incidents(T0, 6 * 3600)
    assert [c["id"] for c in closed] == [iid]
    row = _inc(s, iid)
    assert row["ended_at"] == last, "ended_at 应取最后一条样本时刻（诚实反映最后活动）"
    assert "无新样本" in (row["note"] or "")


def test_recent_sample_is_not_stale(tmp_path):
    s = _store(tmp_path)
    iid = _open(s, _machine(s))
    _put(s, "t1", "n1", "fail", T0 - 30)       # 30 秒前还有样本：仍在失败，不能收口
    assert s.close_stale_incidents(T0, 6 * 3600) == []
    assert _inc(s, iid)["ended_at"] is None


def test_stale_disabled_by_zero(tmp_path):
    s = _store(tmp_path)
    started = T0 - 40 * 3600
    iid = _open(s, _machine(s), ts=started)
    _put(s, "t1", "n1", "fail", started + 30)
    assert s.close_stale_incidents(T0, 0) == []
    assert _inc(s, iid)["ended_at"] is None


def test_stale_keeps_original_reason(tmp_path):
    """收口只写 note，不动 reason_json 的原始证据。"""
    s = _store(tmp_path)
    started = T0 - 40 * 3600
    iid = _open(s, _machine(s), ts=started)
    _put(s, "t1", "n1", "fail", started + 30)
    s.close_stale_incidents(T0, 6 * 3600)
    assert "timeout" in (_inc(s, iid)["reason_json"] or "")


def test_stale_ignores_node_incidents(tmp_path):
    """节点离线事件不该被「陈旧」逻辑收口：节点确实离线就是真的在坏。"""
    s = _store(tmp_path)
    nid, _ = s.register_node("nd", "h", {}, "1", {}, T0, token_id="")
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (T0 - 300, nid))
    s.db.commit()
    s.sweep_offline(60, T0)
    assert s.close_stale_incidents(T0, 1) == []
    row = s.db.execute("SELECT ended_at FROM incidents WHERE kind='node'").fetchone()
    assert row["ended_at"] is None


# --------------------------------------------------------------- 根因 1.2

def test_node_reregister_closes_offline_incident(tmp_path):
    """agent 重启走重新注册恢复时，离线事件原先永远收不了口。"""
    s = _store(tmp_path)
    nid, _ = s.register_node("nd", "h", {}, "1", {}, T0, token_id="")
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (T0 - 300, nid))
    s.db.commit()
    s.sweep_offline(60, T0)
    assert s.db.execute("SELECT ended_at FROM incidents WHERE kind='node'").fetchone()["ended_at"] is None

    s.register_node("nd", "h", {}, "1", {}, T0 + 120, token_id="")
    row = s.db.execute("SELECT ended_at, note FROM incidents WHERE kind='node'").fetchone()
    assert row["ended_at"] is not None, "重新注册恢复后离线事件仍挂着"
    assert "恢复" in (row["note"] or "")


def test_node_heartbeat_closes_offline_incident(tmp_path):
    """纯心跳恢复（原本能收口，作为对照，防止改动破坏这条路径）。"""
    s = _store(tmp_path)
    nid, _ = s.register_node("nd", "h", {}, "1", {}, T0, token_id="")
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (T0 - 300, nid))
    s.db.commit()
    s.sweep_offline(60, T0)
    s.node_touch(nid, {}, T0 + 120)
    row = s.db.execute("SELECT ended_at FROM incidents WHERE kind='node'").fetchone()
    assert row["ended_at"] is not None


def test_node_reregister_is_idempotent_for_closed_incidents(tmp_path):
    s = _store(tmp_path)
    nid, _ = s.register_node("nd", "h", {}, "1", {}, T0, token_id="")
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (T0 - 300, nid))
    s.db.commit()
    s.sweep_offline(60, T0)
    s.register_node("nd", "h", {}, "1", {}, T0 + 120, token_id="")
    first = s.db.execute("SELECT ended_at FROM incidents WHERE kind='node'").fetchone()["ended_at"]
    s.register_node("nd", "h", {}, "1", {}, T0 + 200, token_id="")
    again = s.db.execute("SELECT ended_at FROM incidents WHERE kind='node'").fetchone()["ended_at"]
    assert first == again, "已收口的事件不应被再次改写 ended_at"
