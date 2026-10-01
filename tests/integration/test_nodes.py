"""集成测试：节点管理（详情/改名/标签/级联删除）与任务部分更新（类型相关校验、间隔钳制）。"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app


def make_client(tmp_path):
    from gpm.server.storage import Storage
    cfg = Config({"server": {"database": str(tmp_path / "test.db"), "listen": "127.0.0.1:0"}})
    storage = Storage(str(tmp_path / "test.db"))
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name, tags=None):
    """注册节点并返回 (node_id, token)。"""
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "tags": tags or {},
        "version": "0.1.0", "system": {"os": "test"}})
    assert r.status_code == 200, r.text
    nid = r.json()["node_id"]
    return nid, hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def count(s, table, node_id):
    return s.db.execute(f"SELECT COUNT(*) c FROM {table} WHERE node_id=?", (node_id,)).fetchone()["c"]


# ---------------- 节点管理 ----------------

def test_node_detail_and_update(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, _tok = register(client, cfg, "node-a", {"region": "cn"})

    # 详情：结构完整（标签已解析、系统信息已解析、分配任务/可用率/近期事件字段存在）
    d = client.get(f"/api/nodes/{nid}").json()
    assert d["name"] == "node-a" and d["tags"] == {"region": "cn"}
    assert d["system"] == {"os": "test"}
    assert d["assigned_tasks"] == [] and d["avail_24h"] is None and d["recent_incidents"] == []
    assert client.get("/api/nodes/nope").status_code == 404

    # 改名 + 标签
    r = client.put(f"/api/nodes/{nid}", json={"name": "node-a2", "tags": {"region": "cn", "isp": "telecom"}})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "node-a2" and r.json()["tags"]["isp"] == "telecom"
    assert client.get(f"/api/nodes/{nid}").json()["name"] == "node-a2"

    # 仅改标签（name 不出现 = 不更新）
    r = client.put(f"/api/nodes/{nid}", json={"tags": {}})
    assert r.status_code == 200 and r.json()["name"] == "node-a2" and r.json()["tags"] == {}

    # 只改标签时清空标签不影响名字
    r = client.put(f"/api/nodes/{nid}", json={"name": " node-a3 "})  # 首尾空白被裁剪
    assert r.json()["name"] == "node-a3" and r.json()["tags"] == {}

    # 重名 → 409
    register(client, cfg, "node-b")
    assert client.put(f"/api/nodes/{nid}", json={"name": "node-b"}).status_code == 409
    # 未改名（同名）不算冲突
    assert client.put(f"/api/nodes/{nid}", json={"name": "node-a3"}).status_code == 200

    # 非法载荷 → 422
    assert client.put(f"/api/nodes/{nid}", json={"name": ""}).status_code == 422
    assert client.put(f"/api/nodes/{nid}", json={"name": "   "}).status_code == 422
    assert client.put(f"/api/nodes/{nid}", json={"tags": {"a": {"nested": 1}}}).status_code == 422
    assert client.put(f"/api/nodes/{nid}", json={"bogus": 1}).status_code == 422
    # 不存在的节点 → 404
    assert client.put("/api/nodes/nope", json={"name": "x"}).status_code == 404


def test_node_delete_cascades(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "tmp-node")
    keep_id, _ = register(client, cfg, "keep-node")

    # 任务只分配给 tmp-node
    tid = client.post("/api/tasks", json={
        "name": "ping-tmp", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10, "nodes": [nid]}).json()["id"]
    v0 = s.config_version()

    # 心跳（产生 node_heartbeats）+ 3 次失败（产生事件）
    client.post("/api/agent/sync", json={"node_id": nid, "token": token,
                                         "config_version": 0, "stats": {"cpu": 1.5, "mem": 2.5}})
    ts0 = int(time.time()) - 30
    res = [{"ts": ts0 + i, "task_id": tid, "type": "ping", "dns": "", "url": "",
            "status": "fail", "error_class": "timeout", "metrics": {}} for i in range(3)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})
    from gpm.server.storage import BUCKET_SECONDS
    step = BUCKET_SECONDS["1m"]
    s.agg_recompute("1m", ts0 // step * step, ts0 + 120)

    assert count(s, "probe_results", nid) == 3
    assert count(s, "aggregates", nid) > 0
    assert count(s, "node_heartbeats", nid) >= 1
    assert count(s, "incidents", nid) >= 1

    # 删除 → 级联清理 + 从任务分配移除 + config_version 递增
    r = client.request("DELETE", f"/api/nodes/{nid}")
    assert r.status_code == 200 and r.json()["name"] == "tmp-node"
    for table in ("probe_results", "aggregates", "node_heartbeats", "incidents"):
        assert count(s, table, nid) == 0, f"{table} 未级联清理"
    assert client.get(f"/api/nodes/{nid}").status_code == 404
    assert all(n["id"] != nid for n in client.get("/api/nodes").json())
    assert s.get_task(tid)["nodes"] == [], "应从任务分配中移除"
    assert s.config_version() > v0
    # 其它节点不受影响
    assert any(n["id"] == keep_id for n in client.get("/api/nodes").json())
    # 重复删除 → 404
    assert client.request("DELETE", f"/api/nodes/{nid}").status_code == 404


def test_node_avail_24h_spans_all_streams(tmp_path):
    """可用率必须跨线路/URL 流聚合（历史缺陷：只统计 dns/url 均为空的流）。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "avail-node")
    tid = client.post("/api/tasks", json={
        "name": "ping-lines", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10, "dns": ["223.5.5.5"], "nodes": [nid]}).json()["id"]

    ts0 = int(time.time()) - 30
    res = []
    for i, st in enumerate(["ok", "fail", "ok", "fail"]):
        res.append({"ts": ts0 + i, "task_id": tid, "type": "ping", "dns": "223.5.5.5", "url": "",
                    "status": st, "metrics": {"rtt_avg": 10.0 if st == "ok" else None}})
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})
    from gpm.server.storage import BUCKET_SECONDS
    step = BUCKET_SECONDS["1m"]
    s.agg_recompute("1m", ts0 // step * step, ts0 + 120)

    d = client.get(f"/api/nodes/{nid}").json()
    assert d["avail_24h"] == 0.5, f"应跨线路聚合，实际 {d['avail_24h']}"
    assert [t["id"] for t in d["assigned_tasks"]] == [tid]


# ---------------- 任务部分更新 ----------------

def test_task_update_partial_and_type_checks(tmp_path):
    client, cfg, s = make_client(tmp_path)

    # curl 任务：target 可为空（前端编辑时总会带上 target:""）
    cid = client.post("/api/tasks", json={
        "name": "c1", "type": "curl", "target": "",
        "urls": ["https://example.com/a"]}).json()["id"]
    r = client.put(f"/api/tasks/{cid}", json={
        "name": "c1", "target": "", "urls": ["https://example.com/a", "https://example.com/b"],
        "interval_seconds": 15, "dns": [], "nodes": []})
    assert r.status_code == 200, r.text
    assert r.json()["urls"] == ["https://example.com/a", "https://example.com/b"]
    assert r.json()["interval_seconds"] == 15

    # ping 任务：目标为空 / 非法 / 注入 → 422
    pid = client.post("/api/tasks", json={
        "name": "p1", "type": "ping", "target": "223.5.5.5"}).json()["id"]
    for bad in ("", "1.1.1.1; rm -rf /", "$(whoami)", "not a target"):
        assert client.put(f"/api/tasks/{pid}", json={"target": bad}).status_code == 422, bad
    # curl 任务的 target 不做强校验（不参与探测）
    assert client.put(f"/api/tasks/{cid}", json={"target": ""}).status_code == 200

    # 间隔钳制：ping 下限 10、mtr 下限 60；低于 pydantic 下限直接 422
    assert client.put(f"/api/tasks/{pid}", json={"interval_seconds": 10}).json()["interval_seconds"] == 10
    assert client.put(f"/api/tasks/{pid}", json={"interval_seconds": 3}).status_code == 422
    mid = client.post("/api/tasks", json={
        "name": "m1", "type": "mtr", "target": "8.8.8.8", "interval_seconds": 60}).json()["id"]
    assert client.put(f"/api/tasks/{mid}", json={"interval_seconds": 30}).json()["interval_seconds"] == 60
    assert client.put(f"/api/tasks/{mid}", json={"interval_seconds": 120}).json()["interval_seconds"] == 120

    # 未识别字段被忽略，不改动数据；启用/停用；未知任务 → 404
    before = s.get_task(pid)
    assert client.put(f"/api/tasks/{pid}", json={"bogus": 1}).status_code == 200
    assert s.get_task(pid)["name"] == before["name"]
    assert client.put(f"/api/tasks/{pid}", json={"enabled": False}).json()["enabled"] == 0
    assert client.put(f"/api/tasks/{pid}", json={"enabled": True}).json()["enabled"] == 1
    assert client.put("/api/tasks/nope", json={"name": "x"}).status_code == 404
    assert client.put("/api/tasks/nope", json={"interval_seconds": 10}).status_code == 404

    # 更新必须递增 config_version（节点据此拉取新配置）
    v0 = s.config_version()
    client.put(f"/api/tasks/{pid}", json={"interval_seconds": 30})
    assert s.config_version() > v0


# ---------------- 工具缺失（skipped）如实呈现 ----------------

def test_skipped_streams_surface_as_tool_missing(tmp_path):
    """mtr 未安装这类「工具缺失」必须如实显示：既不进聚合，也不能伪装成「无数据」。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "mtr-node")
    tid = client.post("/api/tasks", json={
        "name": "mtr-x", "type": "mtr", "target": "8.8.8.8",
        "interval_seconds": 60}).json()["id"]

    ts0 = int(time.time()) - 180
    res = [{"ts": ts0 + i * 60, "task_id": tid, "type": "mtr", "dns": "", "url": "",
            "status": "skipped", "error_class": "tool_missing", "error": "mtr 未安装",
            "metrics": {}} for i in range(3)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})

    # 跳过不计入可用率、不产生聚合桶
    t = next(x for x in client.get("/api/tasks").json() if x["id"] == tid)
    assert t["current_status"] == "skipped", "全流 skipped 应如实标注"
    assert "mtr 未安装" in t["skip_reason"]
    assert t["avail_24h"] is None
    assert s.agg_read("1m", tid, nid, "", "", ts0 - 60, ts0 + 600) == []

    # 条带里该流仍要出现（灰格）并带上原因，而不是整行消失
    up = client.get(f"/api/query/uptime?task_id={tid}&bucket=60"
                    f"&t_from={ts0 - 60}&t_to={ts0 + 300}").json()
    assert len(up["rows"]) == 1, "只有 skipped 记录的流也必须在条带中出现"
    row = up["rows"][0]
    assert row["skipped"] == "mtr 未安装" and set(c["st"] for c in row["cells"]) == {2}

    # 流接口同样带上原因，供前端筛选器/tooltip 使用
    st = client.get(f"/api/query/streams?task_id={tid}").json()[0]
    assert st["latest_status"] == "skipped" and st["latest_error_class"] == "tool_missing"


def test_query_mtr_returns_latest_per_stream(tmp_path):
    """mtr 明细按流返回：有跳数的流不能被另一节点较晚的 skipped 记录挤掉。"""
    client, cfg, s = make_client(tmp_path)
    n1, tk1 = register(client, cfg, "mtr-ok")
    n2, tk2 = register(client, cfg, "mtr-skip")
    tid = client.post("/api/tasks", json={
        "name": "mtr-y", "type": "mtr", "target": "8.8.8.8",
        "interval_seconds": 60}).json()["id"]

    ts0 = int(time.time()) - 120
    hops = [{"hop": 1, "host": "172.17.0.1", "loss_pct": 0.0, "snt": 10, "last": 0.1,
             "avg": 0.1, "best": 0.1, "wrst": 0.2, "stdev": 0.0},
            {"hop": 2, "host": "8.8.8.8", "loss_pct": 0.0, "snt": 10, "last": 190.0,
             "avg": 195.0, "best": 180.0, "wrst": 210.0, "stdev": 5.0}]
    client.post("/api/agent/results", json={"node_id": n1, "token": tk1, "results": [
        {"ts": ts0, "task_id": tid, "type": "mtr", "status": "ok",
         "metrics": {"cycles": 10, "hops": hops}}]})
    # 另一节点更晚的一条 skipped：全局按 ts 取最近会把有跳数的流挤掉
    client.post("/api/agent/results", json={"node_id": n2, "token": tk2, "results": [
        {"ts": ts0 + 60, "task_id": tid, "type": "mtr", "status": "skipped",
         "error_class": "tool_missing", "error": "mtr 未安装", "metrics": {}}]})

    r = client.get(f"/api/query/mtr?task_id={tid}").json()
    assert len(r) == 2, "每个流各一条"
    assert {x["node_name"] for x in r} == {"mtr-ok", "mtr-skip"}
    assert r[0]["ts"] >= r[1]["ts"], "按时间倒序"
    with_hops = [x for x in r if (x["metrics"].get("hops") or [])]
    assert len(with_hops) == 1 and len(with_hops[0]["metrics"]["hops"]) == 2
    # node_id 过滤仍然可用
    only = client.get(f"/api/query/mtr?task_id={tid}&node_id={n2}").json()
    assert len(only) == 1 and only[0]["status"] == "skipped"


# ---------------- 节点详情：IP / OS 版本 / 上线时间 ----------------

def test_node_detail_reports_ip_os_and_uptime(tmp_path):
    client, cfg, s = make_client(tmp_path)
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": "ip-node", "register_token": reg,
        "system": {"os": "linux", "release": "6.8.0", "machine": "x86_64",
                   "pretty": "Debian GNU/Linux 13 (trixie)"},
        "local_ip": "10.1.2.3"})
    nid = r.json()["node_id"]
    token = hashlib.sha256(f"ip-node:{reg}".encode()).hexdigest()

    d = client.get(f"/api/nodes/{nid}").json()
    assert d["local_ip"] == "10.1.2.3"
    assert d["egress_ip"], "应记录服务端观测到的出口 IP"
    assert d["system"]["pretty"] == "Debian GNU/Linux 13 (trixie)"
    assert d["created_at"] and d["online_since"] == d["created_at"]
    # 列表接口同样带上这些字段
    assert next(n for n in client.get("/api/nodes").json() if n["id"] == nid)["local_ip"] == "10.1.2.3"

    # 重复注册：刷新 IP/系统信息，但在线期间不重置上线时间
    r2 = client.post("/api/agent/register", json={
        "name": "ip-node", "register_token": reg,
        "system": {"os": "linux", "pretty": "Debian GNU/Linux 13.1"},
        "local_ip": "10.1.2.9"})
    assert r2.json()["node_id"] == nid and r2.json()["created"] is False
    d = client.get(f"/api/nodes/{nid}").json()
    assert d["local_ip"] == "10.1.2.9" and d["system"]["pretty"] == "Debian GNU/Linux 13.1"
    assert d["online_since"] == d["created_at"], "在线期间重复注册不应重置上线时间"

    # 反代场景：X-Forwarded-For 第一跳作为出口 IP
    client.post("/api/agent/sync", json={"node_id": nid, "token": token, "config_version": 0},
                headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})
    assert client.get(f"/api/nodes/{nid}").json()["egress_ip"] == "203.0.113.7"

    # 离线 → 回归：上线时间重置为本次上线时刻
    old_since = d["last_heartbeat"] - 500          # 模拟「已经在线很久」
    s.db.execute("UPDATE nodes SET online_since=? WHERE id=?", (old_since, nid))
    s.db.commit()
    s.sweep_offline(60, d["last_heartbeat"] + 3600)   # 判定离线
    assert s.node_by_id(nid)["status"] == "offline"
    client.post("/api/agent/sync", json={"node_id": nid, "token": token, "config_version": 0,
                                         "stats": {"local_ip": "10.1.2.9"}})
    d2 = client.get(f"/api/nodes/{nid}").json()
    assert d2["status"] == "online"
    assert d2["online_since"] > old_since, "离线后回归应重置上线时间"
    assert d2["online_since"] == d2["last_heartbeat"]
