"""值班总览的聚合降噪（.docs/ONCALL_OPTIMIZATION_2.md 第二期 7-10）。

现场问题：线上 11 条事件里同一任务占多条（curl-baidu-multi 3 条 URL、ping-223 2 个节点），
值班的人得在第一屏手动合并；而「同一节点上多个任务同时失败」这个最有价值的**根因提示**
完全没有暴露。这里把分组、横切、排序、维护窗口、筛选口径都固化成用例。
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "g.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name):
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "version": "0.1.0", "system": {}})
    assert r.status_code == 200, r.text
    return r.json()["node_id"], hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def make_task(client, name, ttype="ping", target="1.1.1.1", urls=None):
    body = {"name": name, "type": ttype, "target": target, "interval_seconds": 10}
    if urls:
        body["urls"] = urls
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def fail_stream(client, nid, token, tid, ts0, dns="", url="", n=3):
    """连续 n 次失败 → 达到 fail_threshold 开事件。"""
    res = [{"ts": ts0 + i * 10, "task_id": tid, "type": "ping", "dns": dns, "url": url,
            "status": "fail", "error_class": "timeout", "metrics": {}} for i in range(n)]
    r = client.post("/api/agent/results", json={"node_id": nid, "token": token,
                                                "results": res})
    assert r.status_code == 200, r.text


def oncall(client):
    r = client.get("/api/oncall")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 按任务聚合

def test_same_task_streams_collapse_into_one_group(tmp_path):
    """同任务的 3 条 URL 流 → 一张卡（count=3），底层 3 条事件都在 members 里可展开。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "multi-url", "curl", "",
                    urls=["https://a/", "https://b/", "https://c/"])
    ts0 = int(time.time()) - 60
    for u in ("https://a/", "https://b/", "https://c/"):
        fail_stream(client, nid, token, tid, ts0, url=u)

    d = oncall(client)
    assert len(d["items"]) == 3
    groups = d["groups"]
    assert len(groups) == 1, groups
    g = groups[0]
    assert g["kind"] == "task" and g["task_id"] == tid
    assert g["count"] == 3 and len(g["members"]) == 3
    assert sorted(g["targets"]) == ["https://a/", "https://b/", "https://c/"]
    assert "3 条流" in g["subtitle"] and "3 个目标" in g["subtitle"]
    assert len(g["incident_ids"]) == 3


def test_different_tasks_stay_separate(tmp_path):
    """不同任务不能合并：否则一屏只剩一张卡，反而看不出坏了几个目标。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    t1 = make_task(client, "a")
    t2 = make_task(client, "b")
    ts0 = int(time.time()) - 60
    fail_stream(client, nid, token, t1, ts0)
    fail_stream(client, nid, token, t2, ts0 + 5)
    groups = oncall(client)["groups"]
    assert len(groups) == 2
    assert {g["task_id"] for g in groups} == {t1, t2}


# ---------------------------------------------------------------- 节点横切（根因提示）

def test_node_suspect_crosscut_when_many_tasks_fail_on_one_node(tmp_path):
    """同一节点上 ≥3 个任务同时失败 → 置顶的「疑似节点侧」横切卡。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "busy-node")
    ts0 = int(time.time()) - 60
    tids = [make_task(client, "t%d" % i) for i in range(3)]
    for i, tid in enumerate(tids):
        fail_stream(client, nid, token, tid, ts0 + i)

    groups = oncall(client)["groups"]
    suspects = [g for g in groups if g["kind"] == "node_suspect"]
    assert len(suspects) == 1, groups
    sus = suspects[0]
    assert sus["node_id"] == nid and sus["count"] == 3
    assert "疑似节点侧" in sus["title"]
    assert "优先查节点出口/资源" in sus["advice"]
    # 必须排在第一位：这是最有价值的根因提示
    assert groups[0]["kind"] == "node_suspect", [g["kind"] for g in groups]


def test_no_crosscut_below_threshold(tmp_path):
    """只有 2 个任务失败时不给横切提示：证据不足，提示会变成噪声。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    ts0 = int(time.time()) - 60
    for i in range(2):
        fail_stream(client, nid, token, make_task(client, "t%d" % i), ts0 + i)
    groups = oncall(client)["groups"]
    assert not [g for g in groups if g["kind"] == "node_suspect"]


def test_crosscut_ignores_tasks_on_other_nodes(tmp_path):
    """横切按节点分别统计：3 个任务分散在 3 个节点上不算节点侧。"""
    client, cfg, s = make_client(tmp_path)
    ts0 = int(time.time()) - 60
    for i in range(3):
        nid, token = register(client, cfg, "node%d" % i)
        fail_stream(client, nid, token, make_task(client, "t%d" % i), ts0 + i)
    groups = oncall(client)["groups"]
    assert not [g for g in groups if g["kind"] == "node_suspect"]


# ---------------------------------------------------------------- 排序

def test_groups_sorted_by_bucket_then_impact(tmp_path):
    """排序：横切置顶 → 分档（正在失败 > 沉默 > 陈旧）→ 影响面（流数）→ 时长。

    原先是按事件开始时间倒序，「现在真在坏、且影响面大」的那条不一定在最上面。
    """
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    ts0 = int(time.time()) - 60
    # 任务 A：3 条流正在失败（影响面大）
    ta = make_task(client, "A")
    for u in ("https://a1/", "https://a2/", "https://a3/"):
        fail_stream(client, nid, token, ta, ts0, url=u)
    # 任务 B：1 条流正在失败（影响面小）
    tb = make_task(client, "B")
    fail_stream(client, nid, token, tb, ts0 + 5)

    groups = [g for g in oncall(client)["groups"] if g["kind"] == "task"]
    assert [g["task_id"] for g in groups] == [ta, tb], [(g["task_id"], g["count"]) for g in groups]


# ---------------------------------------------------------------- 维护窗口

def test_maintenance_window_marked_and_grouped(tmp_path):
    """维护窗口内的事件必须显式标注，不再以「正在失败」占着第一屏。

    in_maintenance() 早已存在，但此前只作用于告警评估——值班页不认它。
    """
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "maint-task")
    now = int(time.time())
    s.create_window("w1", {"name": "计划内升级", "starts_at": now - 600,
                           "ends_at": now + 600, "task_id": tid}, now)
    fail_stream(client, nid, token, tid, now - 60)

    d = oncall(client)
    assert d["items"][0]["maintenance"], "item 没带上维护窗口信息"
    assert d["items"][0]["maintenance"]["name"] == "计划内升级"
    g = d["groups"][0]
    assert g["maintenance"] and g["maintenance"]["name"] == "计划内升级"


def test_no_maintenance_field_when_outside_window(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    now = int(time.time())
    s.create_window("w1", {"name": "过期窗口", "starts_at": now - 7200,
                           "ends_at": now - 3600, "task_id": tid}, now)
    fail_stream(client, nid, token, tid, now - 60)
    assert oncall(client)["items"][0]["maintenance"] is None


# ---------------------------------------------------------------- 确认与向后兼容

def test_group_acked_only_when_all_members_acked(tmp_path):
    """聚合卡的「已确认」= 卡内每条都确认过；只确认一条不算整个行动项已认领。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    ts0 = int(time.time()) - 60
    for u in ("https://a/", "https://b/"):
        fail_stream(client, nid, token, tid, ts0, url=u)
    g = oncall(client)["groups"][0]
    assert g["count"] == 2 and g["acked"] is False

    first = g["incident_ids"][0]
    client.post(f"/api/event/{first}/ack", json={"note": "x"})
    assert oncall(client)["groups"][0]["acked"] is False   # 还有一条没确认
    second = g["incident_ids"][1]
    client.post(f"/api/event/{second}/ack", json={"note": "x"})
    assert oncall(client)["groups"][0]["acked"] is True


def test_items_kept_for_backward_compatibility(tmp_path):
    """items 必须保持原样返回：前端旧逻辑与既有验收断言都依赖它。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    fail_stream(client, nid, token, tid, int(time.time()) - 60)
    d = oncall(client)
    assert d["items"] and d["groups"]
    assert d["items"][0]["incident_id"] == d["groups"][0]["incident_ids"][0]


def test_groups_empty_when_nothing_open(tmp_path):
    client, cfg, s = make_client(tmp_path)
    register(client, cfg, "n1")
    make_task(client, "t")
    d = oncall(client)
    assert d["items"] == [] and d["groups"] == []
