"""storage 事件（incidents）生命周期与 zombie_incidents 自查单测：

- incident_open：每次调用都新开一行（本层不去重，复用与否由上层经 open_incident_for 决定）；
- incident_close：置 ended_at/note/duration_ms（时钟回拨钳 0、空 note 不覆盖已有 note），
  幂等——重复关/不存在 id 返回 False；last_closed_incident 取同流 ended_at 最近的一条；
- incident_reopen：只对已关事件生效（ended_at 清空、reopen_count+1、reason 整体替换），
  对 open 事件/不存在 id 返回 False，重开后再次收口时长从原 started_at 起算；
- open_incident_for：按 (task,node,dns,url) 四元组精确匹配 open 事件，无则 None；
- zombie_incidents：三类「不可信事件」——stale_ok（该流最近样本已 ok）/ disabled
  （任务已停用却仍开着事件）/ node_online（节点在线却挂着离线事件）；limit 按类各自
  截断；已关事件与不满足条件者（最近样本 fail、无样本、任务不存在、节点离线）不出现；
- streams_with_open_incidents：仅 open 且 kind='probe' 的流，节点侧事件不算；
- close_task_incidents / close_node_incidents：按任务收探测事件、按节点收节点事件，
  返回关闭数量；种类不符/无关任务/未知 id 返回 0；
- close_stale_incidents：以「该流最后一条样本」判沉默——last_ts < ts-stale_after 才收，
  ended_at 取 max(started_at, last_ts)，stale_after<=0 视为机制关闭，limit 限制候选数
  （最早开始优先），重复调用幂等，节点侧事件不受影响；
- list_incidents：started_at 倒序、limit、open_only、t_from/t_to 窗口过滤、reason 解析。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server.storage import Storage

T0 = 1_700_000_000
URL = "https://example.com"


def make_storage(tmp_path):
    s = Storage(str(tmp_path / "zombie.db"))
    s.create_task("t1", "任务一", "curl", URL, [URL], {}, [], 30, T0)
    s.create_task("t2", "任务二", "curl", URL, [URL], {}, [], 30, T0)
    for name in ("北京", "上海", "广州"):
        s.register_node(name, "h", {}, "v1", {}, T0)
    return s


def _nid(s, name):
    return next(n["id"] for n in s.list_nodes() if n["name"] == name)


def _open(s, task_id="t1", node="北京", dns="", url=URL, started=T0, reason=None):
    return s.incident_open(task_id, _nid(s, node), dns, url, started,
                           reason or {"event": "fail", "error_class": "timeout"})


def _res(s, ts, node, status, task_id="t1", dns="", url=URL):
    return {"ts": ts, "task_id": task_id, "node_id": _nid(s, node), "type": "curl",
            "status": status, "error_class": "" if status == "ok" else "timeout",
            "error": "", "dns": dns, "url": url, "metrics": {}}


def json_reason(row):
    return json.loads(row["reason_json"])


# ---------------- incident_open / open_incident_for ----------------

def test_incident_open_new_row_and_fields(tmp_path):
    s = make_storage(tmp_path)
    iid = _open(s, started=T0 + 5)
    assert isinstance(iid, int) and iid >= 1
    r = s.open_incident_for("t1", _nid(s, "北京"), "", URL)
    assert r["id"] == iid and r["task_id"] == "t1" and r["node_id"] == _nid(s, "北京")
    assert r["dns"] == "" and r["url"] == URL
    assert r["started_at"] == T0 + 5 and r["ended_at"] is None
    assert r["kind"] == "probe" and r["duration_ms"] is None
    assert json_reason(r) == {"event": "fail", "error_class": "timeout"}


def test_incident_open_same_stream_inserts_new_row(tmp_path):
    s = make_storage(tmp_path)
    i1 = _open(s, started=T0)
    i2 = _open(s, started=T0 + 60)     # 同流再开：本层不做合并，直接新开一行
    assert i2 != i1 and i2 > i1
    assert len(s.list_incidents()) == 2
    # open_incident_for 取 started_at 最近的一条 open 事件
    assert s.open_incident_for("t1", _nid(s, "北京"), "", URL)["id"] == i2


def test_open_incident_for_stream_scoped(tmp_path):
    s = make_storage(tmp_path)
    assert s.open_incident_for("t1", _nid(s, "北京"), "", URL) is None   # 还没有事件
    iid = _open(s, dns="", url=URL)
    # 四元组任一不同即视为另一条流
    assert s.open_incident_for("t1", _nid(s, "北京"), "", URL)["id"] == iid
    assert s.open_incident_for("t1", _nid(s, "北京"), "", "https://other.com") is None
    assert s.open_incident_for("t1", _nid(s, "上海"), "", URL) is None
    assert s.open_incident_for("t2", _nid(s, "北京"), "", URL) is None
    s.incident_close(iid, T0 + 30, "")
    assert s.open_incident_for("t1", _nid(s, "北京"), "", URL) is None   # 只看 open


# ---------------- incident_close / last_closed_incident ----------------

def test_incident_close_and_last_closed(tmp_path):
    s = make_storage(tmp_path)
    i1 = _open(s, started=T0)
    assert s.incident_close(i1, T0 + 120, "已恢复") is True
    r = s.last_closed_incident("t1", _nid(s, "北京"), "", URL)
    assert r["id"] == i1 and r["ended_at"] == T0 + 120
    assert r["duration_ms"] == 120_000 and r["note"] == "已恢复"
    # 收口只写 note，不动 reason_json 里的原始证据
    assert json_reason(r) == {"event": "fail", "error_class": "timeout"}
    # 同流再开再关：last_closed 取 ended_at 最近的一条
    i2 = _open(s, started=T0 + 200)
    s.incident_close(i2, T0 + 300, "")
    r2 = s.last_closed_incident("t1", _nid(s, "北京"), "", URL)
    assert r2["id"] == i2 and r2["ended_at"] == T0 + 300
    # 两条都已关 → open 查询为空；无事件的流查不到最近关闭事件
    assert s.open_incident_for("t1", _nid(s, "北京"), "", URL) is None
    assert s.last_closed_incident("t2", _nid(s, "上海"), "", URL) is None


def test_incident_close_edge_cases(tmp_path):
    s = make_storage(tmp_path)
    # 时钟回拨：ended_at 如实记录，duration 钳为 0 不出现负数
    i1 = _open(s, started=T0 + 1000)
    assert s.incident_close(i1, T0 + 900, "") is True
    r = s.last_closed_incident("t1", _nid(s, "北京"), "", URL)
    assert r["ended_at"] == T0 + 900 and r["duration_ms"] == 0
    # 幂等：重复关已关事件 → False，且 ended_at/note 不被改写
    assert s.incident_close(i1, T0 + 5000, "again") is False
    assert s.last_closed_incident("t1", _nid(s, "北京"), "", URL)["ended_at"] == T0 + 900
    # 不存在的 id → False
    assert s.incident_close(99999, T0 + 10, "") is False
    # 空 note 不覆盖已有 note（reopen 后二次收口验证）
    i2 = _open(s, node="上海", started=T0)
    s.incident_close(i2, T0 + 60, "第一次收口")
    s.incident_reopen(i2, T0 + 120, {"event": "jitter"})
    s.incident_close(i2, T0 + 180, "")
    assert s.last_closed_incident("t1", _nid(s, "上海"), "", URL)["note"] == "第一次收口"


# ---------------- incident_reopen ----------------

def test_incident_reopen(tmp_path):
    s = make_storage(tmp_path)
    i1 = _open(s, started=T0)
    assert s.incident_reopen(i1, T0 + 10, {}) is False        # open 事件不适用
    assert s.incident_reopen(99999, T0 + 10, {}) is False     # 不存在 id
    s.incident_close(i1, T0 + 100, "")
    assert s.incident_reopen(i1, T0 + 200, {"event": "jitter", "n": 1}) is True
    r = s.open_incident_for("t1", _nid(s, "北京"), "", URL)
    assert r["id"] == i1 and r["ended_at"] is None and r["reopen_count"] == 1
    assert json_reason(r) == {"event": "jitter", "n": 1}      # reason 整体替换
    # 仍处 open → 再 reopen 无效
    assert s.incident_reopen(i1, T0 + 300, {}) is False
    # 再次收口：时长从原 started_at 起算（同一事件累计），reopen_count 保持 1
    s.incident_close(i1, T0 + 400, "")
    r2 = s.last_closed_incident("t1", _nid(s, "北京"), "", URL)
    assert r2["duration_ms"] == 400_000 and r2["reopen_count"] == 1


# ---------------- zombie_incidents ----------------

def test_zombie_stale_ok(tmp_path):
    s = make_storage(tmp_path)
    # 是僵尸：事件开着，该流最近一条样本已是 ok（该被恢复收口而没收）
    i1 = _open(s, node="北京")
    s.insert_results([_res(s, T0 + 60, "北京", "ok")], T0 + 60)
    # 不是僵尸：最近样本仍是 fail
    i2 = _open(s, node="上海")
    s.insert_results([_res(s, T0 + 60, "上海", "fail")], T0 + 60)
    # 不是僵尸：该流从未产生样本（沉默≠ok）
    i3 = _open(s, task_id="t2", node="北京", url="https://other.com")
    z = s.zombie_incidents()
    assert set(z) == {"stale_ok", "disabled", "node_online"}
    assert z["stale_ok"] == [i1]
    assert i2 not in z["stale_ok"] and i3 not in z["stale_ok"]
    assert z["disabled"] == [] and z["node_online"] == []
    # 看的是「最近一条」：fail 翻成 ok 后，对应事件也算僵尸
    s.insert_results([_res(s, T0 + 120, "上海", "ok")], T0 + 120)
    assert set(s.zombie_incidents()["stale_ok"]) == {i1, i2}
    # 已关事件不再算僵尸
    s.incident_close(i1, T0 + 150, "")
    assert s.zombie_incidents()["stale_ok"] == [i2]


def test_zombie_disabled(tmp_path):
    s = make_storage(tmp_path)
    s.create_task("t-off", "停用任务", "curl", URL, [URL], {}, [], 30, T0)
    s.update_task("t-off", {"enabled": 0}, T0 + 5)      # 先停用（此刻无事件可收）
    i_off1 = _open(s, task_id="t-off", node="北京", url="https://off.com")
    i_off2 = _open(s, task_id="t-off", node="上海", url="https://off.com")
    i_on = _open(s, task_id="t1", node="北京")           # 启用中的任务
    i_ghost = _open(s, task_id="t-ghost", node="广州")   # 任务已不存在 → JOIN 不命中
    z = s.zombie_incidents()
    assert set(z["disabled"]) == {i_off1, i_off2}
    assert i_on not in z["disabled"] and i_ghost not in z["disabled"]
    assert z["stale_ok"] == [] and z["node_online"] == []
    # 已关事件不再算僵尸
    s.incident_close(i_off1, T0 + 20, "人工收口")
    assert s.zombie_incidents()["disabled"] == [i_off2]


def test_zombie_node_online(tmp_path):
    s = make_storage(tmp_path)
    bj = _nid(s, "北京")
    # 上海心跳停更 → sweep 判离线并留下一条 open 的节点事件（节点确实离线，不算僵尸）
    s.node_touch(bj, {}, T0 + 100)                      # 北京心跳刷新，保持在线
    s.node_touch(_nid(s, "广州"), {}, T0 + 100)          # 广州同理，避免被 sweep 带走
    assert s.sweep_offline(60, T0 + 150) == 1           # 只有上海被标记离线
    assert s.zombie_incidents()["node_online"] == []
    # 北京在线却挂着 open 的节点事件 → 僵尸
    bj_inc = s.node_incident_open(bj, T0 + 160, {"event": "offline-test"})
    z = s.zombie_incidents()
    assert z["node_online"] == [bj_inc]
    assert z["stale_ok"] == [] and z["disabled"] == []
    # 节点侧事件按节点幂等：再开复用同一行
    assert s.node_incident_open(bj, T0 + 170, {}) == bj_inc
    # 心跳恢复 → 自动收口，僵尸消失
    s.node_touch(bj, {}, T0 + 200)
    assert s.zombie_incidents()["node_online"] == []
    rows = {r["id"]: r for r in s.list_incidents()}
    assert rows[bj_inc]["ended_at"] == T0 + 200
    assert rows[bj_inc]["note"] == "自动收口：节点心跳恢复"


def test_zombie_limit_per_category(tmp_path):
    s = make_storage(tmp_path)
    s.create_task("t-off", "停用任务", "curl", URL, [URL], {}, [], 30, T0)
    s.update_task("t-off", {"enabled": 0}, T0 + 5)
    i1 = _open(s, node="北京")
    s.insert_results([_res(s, T0 + 60, "北京", "ok")], T0 + 60)
    i2 = _open(s, node="上海")
    s.insert_results([_res(s, T0 + 60, "上海", "ok")], T0 + 60)
    d1 = _open(s, task_id="t-off", node="北京", url="https://off.com")
    d2 = _open(s, task_id="t-off", node="上海", url="https://off.com")
    # limit 按类各自截断（类内无排序，只断言条数与来源）
    z = s.zombie_incidents(limit=1)
    assert len(z["stale_ok"]) == 1 and z["stale_ok"][0] in (i1, i2)
    assert len(z["disabled"]) == 1 and z["disabled"][0] in (d1, d2)
    # 默认 limit=50 不截断
    z = s.zombie_incidents()
    assert set(z["stale_ok"]) == {i1, i2} and set(z["disabled"]) == {d1, d2}


# ---------------- streams_with_open_incidents ----------------

def test_streams_with_open_incidents(tmp_path):
    s = make_storage(tmp_path)
    assert s.streams_with_open_incidents() == []
    bj, sh = _nid(s, "北京"), _nid(s, "上海")
    i1 = _open(s, task_id="t1", node="北京", dns="", url=URL)
    _open(s, task_id="t1", node="上海", dns="", url=URL)
    _open(s, task_id="t2", node="北京", dns="8.8.8.8", url="")
    got = {(d["task_id"], d["node_id"], d["dns"], d["url"])
           for d in s.streams_with_open_incidents()}
    assert got == {("t1", bj, "", URL), ("t1", sh, "", URL), ("t2", bj, "8.8.8.8", "")}
    # 收口一条 → 对应流消失
    s.incident_close(i1, T0 + 30, "")
    got = {(d["task_id"], d["node_id"], d["dns"], d["url"])
           for d in s.streams_with_open_incidents()}
    assert ("t1", bj, "", URL) not in got and len(got) == 2
    # 节点侧事件（kind='node'）不算探测流
    s.node_incident_open(bj, T0 + 60, {"event": "offline"})
    got = {(d["task_id"], d["node_id"], d["dns"], d["url"])
           for d in s.streams_with_open_incidents()}
    assert ("", bj, "", "") not in got and len(got) == 2


# ---------------- close_task_incidents / close_node_incidents ----------------

def test_close_task_incidents(tmp_path):
    s = make_storage(tmp_path)
    i1 = _open(s, task_id="t1", node="北京")
    i2 = _open(s, task_id="t1", node="上海")
    i3 = _open(s, task_id="t2", node="北京")
    assert s.close_task_incidents("t1", T0 + 100, "任务停用") == 2
    assert s.last_closed_incident("t1", _nid(s, "北京"), "", URL)["id"] == i1
    r2 = s.last_closed_incident("t1", _nid(s, "上海"), "", URL)
    assert r2["id"] == i2 and r2["note"] == "任务停用"
    # 其他任务不受影响；再关一次与未知任务都无事可做
    assert s.open_incident_for("t2", _nid(s, "北京"), "", URL)["id"] == i3
    assert s.close_task_incidents("t1", T0 + 200, "") == 0
    assert s.close_task_incidents("t-none", T0 + 200, "") == 0


def test_close_node_incidents(tmp_path):
    s = make_storage(tmp_path)
    bj = _nid(s, "北京")
    n_inc = s.node_incident_open(bj, T0 + 10, {"event": "offline"})
    p_inc = _open(s, task_id="t1", node="北京")   # 同节点的探测事件：kind 不同不连带
    assert s.close_node_incidents(bj, T0 + 100, "节点下线维护") == 1
    rows = {r["id"]: r for r in s.list_incidents()}
    assert rows[n_inc]["ended_at"] == T0 + 100 and rows[n_inc]["note"] == "节点下线维护"
    assert rows[p_inc]["ended_at"] is None
    # 再关一次/未知节点 → 0；探测事件仍归任务收口管
    assert s.close_node_incidents(bj, T0 + 200, "") == 0
    assert s.close_node_incidents("n-none", T0 + 200, "") == 0
    assert s.close_task_incidents("t1", T0 + 300, "") == 1


# ---------------- close_stale_incidents ----------------

def test_close_stale_incidents(tmp_path):
    s = make_storage(tmp_path)
    bj, sh = _nid(s, "北京"), _nid(s, "上海")
    # A：最后一条样本在 T0（陈旧）→ ended_at 取 last_ts
    i_a = _open(s, task_id="t1", node="北京", started=T0 - 100)
    s.insert_results([_res(s, T0, "北京", "fail")], T0)
    # B：近期还有样本 → 不算沉默
    i_b = _open(s, task_id="t1", node="上海", started=T0)
    s.insert_results([_res(s, T0 + 500, "上海", "fail")], T0 + 500)
    # C：从未有样本 → ended_at 退回 started_at
    i_c = _open(s, task_id="t2", node="北京", url="https://other.com", started=T0 + 10)
    n_inc = s.node_incident_open(_nid(s, "广州"), T0 + 20, {"event": "offline"})

    closed = s.close_stale_incidents(T0 + 1000, stale_after=600)   # cutoff=T0+400
    assert {c["id"] for c in closed} == {i_a, i_c}
    by = {c["id"]: c for c in closed}
    assert by[i_a]["task_id"] == "t1" and by[i_a]["node_id"] == bj
    assert by[i_a]["ended_at"] == T0 and by[i_a]["last_ts"] == T0
    assert by[i_c]["ended_at"] == T0 + 10 and by[i_c]["last_ts"] == 0
    # B 仍开着；A/C 落库字段正确
    assert s.open_incident_for("t1", sh, "", URL)["id"] == i_b
    rows = {r["id"]: r for r in s.list_incidents()}
    assert rows[i_a]["note"].startswith("自动收口")
    assert rows[i_a]["duration_ms"] == 100_000
    assert rows[i_c]["ended_at"] == T0 + 10
    assert rows[n_inc]["ended_at"] is None            # 节点侧事件不归它管
    # 幂等：再跑一遍无事可做
    assert s.close_stale_incidents(T0 + 1000, stale_after=600) == []
    # stale_after<=0 = 机制关闭：什么都不关
    assert s.close_stale_incidents(T0 + 1000, stale_after=0) == []
    assert s.close_stale_incidents(T0 + 1000, stale_after=-1) == []
    assert s.open_incident_for("t1", sh, "", URL)["id"] == i_b


def test_close_stale_incidents_limit(tmp_path):
    s = make_storage(tmp_path)
    i1 = _open(s, task_id="t1", node="北京", started=T0)
    i2 = _open(s, task_id="t1", node="上海", started=T0 + 10)
    i3 = _open(s, task_id="t2", node="北京", started=T0 + 20)
    # limit 限制候选数，最早开始的优先处理
    closed = s.close_stale_incidents(T0 + 1000, stale_after=600, limit=1)
    assert [c["id"] for c in closed] == [i1]
    assert s.open_incident_for("t1", _nid(s, "上海"), "", URL)["id"] == i2
    assert s.open_incident_for("t2", _nid(s, "北京"), "", URL)["id"] == i3
    # 不限 limit → 其余陈旧事件全部收口
    closed = s.close_stale_incidents(T0 + 1000, stale_after=600, limit=500)
    assert {c["id"] for c in closed} == {i2, i3}


# ---------------- list_incidents ----------------

def test_list_incidents_limit_and_open_only(tmp_path):
    s = make_storage(tmp_path)
    i1 = _open(s, task_id="t1", node="北京", started=T0)
    i2 = _open(s, task_id="t1", node="上海", started=T0 + 10)
    i3 = _open(s, task_id="t2", node="北京", started=T0 + 20, reason={"event": "x"})
    s.incident_close(i2, T0 + 30, "")
    rows = s.list_incidents()
    assert [r["id"] for r in rows] == [i3, i2, i1]            # started_at 倒序
    assert rows[0]["reason"] == {"event": "x"} and "reason_json" not in rows[0]
    assert rows[0]["reopen_count"] == 0 and rows[0]["kind"] == "probe"
    assert [r["id"] for r in s.list_incidents(limit=2)] == [i3, i2]
    assert [r["id"] for r in s.list_incidents(open_only=True)] == [i3, i1]
    assert [r["id"] for r in s.list_incidents(limit=1, open_only=True)] == [i3]


def test_list_incidents_window(tmp_path):
    s = make_storage(tmp_path)
    i_x = _open(s, task_id="t1", node="北京", started=T0)
    s.incident_close(i_x, T0 + 100, "")
    i_y = _open(s, task_id="t1", node="上海", started=T0 + 200)
    i_z = _open(s, task_id="t2", node="北京", started=T0 + 300)
    # t_from：已关事件按 ended_at 与窗口有交集判断，open 事件恒保留
    assert {r["id"] for r in s.list_incidents(t_from=T0 + 150)} == {i_y, i_z}
    assert {r["id"] for r in s.list_incidents(t_from=T0 + 50)} == {i_x, i_y, i_z}
    # t_to：按 started_at 判断
    assert {r["id"] for r in s.list_incidents(t_to=T0 + 250)} == {i_x, i_y}
    # 组合取交集
    assert {r["id"] for r in s.list_incidents(t_from=T0 + 150, t_to=T0 + 250)} == {i_y}
