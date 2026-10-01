"""集成测试：TestClient 全链路 —— 注册→心跳/配置下发→批量上报（去重/偏差）→聚合→事件→查询/导出。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "test.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    app = create_app(cfg, storage)
    return TestClient(app), cfg, storage


def test_full_pipeline(tmp_path):
    client, cfg, s = make_client(tmp_path)
    reg_token = cfg.agent["register_token"]

    # 1. 注册（幂等）
    r1 = client.post("/api/agent/register", json={
        "name": "node-a", "register_token": reg_token, "tags": {"region": "test"}})
    assert r1.status_code == 200
    nid = r1.json()["node_id"]
    r2 = client.post("/api/agent/register", json={
        "name": "node-a", "register_token": reg_token})
    assert r2.json()["node_id"] == nid and r2.json()["created"] is False

    token = __import__("hashlib").sha256(f"node-a:{reg_token}".encode()).hexdigest()

    # 2. 错误凭据被拒
    assert client.post("/api/agent/sync", json={
        "node_id": nid, "token": "bad", "config_version": 0}).status_code == 401

    # 3. 建任务（含 DNS 线路）→ config_version 递增 → sync 下发
    t = client.post("/api/tasks", json={
        "name": "ping-x", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10, "dns": ["223.5.5.5"]}).json()
    tid = t["id"]
    sync = client.post("/api/agent/sync", json={
        "node_id": nid, "token": token, "config_version": 0}).json()
    assert sync["tasks"] and sync["tasks"][0]["id"] == tid
    v = sync["config_version"]
    # 版本一致 → 不再下发
    sync2 = client.post("/api/agent/sync", json={
        "node_id": nid, "token": token, "config_version": v}).json()
    assert sync2.get("tasks") is None

    # 4. 批量上报：3 次失败 → 事件开启；2 次成功 → 事件恢复；重复上报去重
    ts0 = int(time.time()) - 60
    def batch(results):
        return client.post("/api/agent/results", json={
            "node_id": nid, "token": token, "results": results}).json()

    def res(ts, status, rtt=None):
        return {"ts": ts, "task_id": tid, "type": "ping", "dns": "223.5.5.5", "url": "",
                "status": status, "error_class": "" if status == "ok" else "timeout",
                "dns_server": "223.5.5.5", "resolved_ip": "223.5.5.5", "dns_time_ms": None,
                "metrics": {"sent": 4, "received": 4 if status == "ok" else 0,
                            "loss_rate": 0.0 if status == "ok" else 1.0,
                            "rtt_min": rtt, "rtt_avg": rtt, "rtt_max": rtt},
                "config_version": v}

    b = batch([res(ts0 + i, "fail") for i in range(3)])
    assert b["accepted"] == 3
    incs = client.get("/api/query/incidents?open_only=true").json()
    assert any(i["task_id"] == tid for i in incs), "连续 3 次失败应开启事件"

    b2 = batch([res(ts0 + 30 + i, "ok", 15.0 + i) for i in range(2)])
    incs = client.get("/api/query/incidents?open_only=true").json()
    assert not any(i["task_id"] == tid for i in incs), "连续 2 次成功应恢复事件"

    # 5. 去重：同一批重复上报
    dups = batch([res(ts0 + 30, "ok", 15.0), res(ts0 + 31, "ok", 16.0)])
    assert dups["duplicates"] == 2 and dups["accepted"] == 0

    # 6. 时钟偏差：偏差 >120s 仍入库（原始表）
    skew = batch([{"ts": ts0 - 3600, "task_id": tid, "type": "ping", "dns": "", "url": "",
                   "status": "ok", "metrics": {"rtt_avg": 10.0}, "config_version": v}])
    assert skew["accepted"] == 1

    # 7. 聚合：手动触发 1m 重算并读取
    from gpm.server.storage import BUCKET_SECONDS
    step = BUCKET_SECONDS["1m"]
    b_from = (ts0 // step) * step
    s.agg_recompute("1m", b_from, ts0 + 120)
    rows = s.agg_read("1m", tid, nid, "223.5.5.5", "", b_from, ts0 + 120)
    assert rows, "应产出聚合桶"
    bucket_ok = sum(r["ok"] for r in rows)
    bucket_n = sum(r["count"] for r in rows)
    assert bucket_ok == 2 and bucket_n == 5  # 3 fail + 2 ok（skewed 行在窗口外）

    # 8. 查询 API
    series = client.get(f"/api/query/series?task_id={tid}&node_id={nid}&dns=223.5.5.5&url=&"
                        f"metric=rtt&granularity=raw&t_from={ts0 - 10}&t_to={ts0 + 120}").json()
    assert len(series["points"]) == 5
    up = client.get(f"/api/query/uptime?task_id={tid}&bucket=60&t_from={b_from}&t_to={ts0 + 120}").json()
    assert up["rows"], "通断条带应有行"

    # 9. 明细与导出
    detail = client.get(f"/api/detail?task_id={tid}&node_id={nid}&dns=223.5.5.5&url=&ts={ts0 + 30}").json()
    assert detail["status"] == "ok" and detail["metrics"]["rtt_avg"] == 15.0
    csv_text = client.get(f"/api/export?task_id={tid}&fmt=csv&t_from={ts0 - 10}&t_to={ts0 + 120}").text
    assert "dns_server" in csv_text and "223.5.5.5" in csv_text

    # 10. 任务更新 → 版本递增 → 删除
    client.put(f"/api/tasks/{tid}", json={"interval_seconds": 30})
    assert s.config_version() > v
    client.request("DELETE", f"/api/tasks/{tid}")
    assert all(x["id"] != tid for x in client.get("/api/tasks").json())


def test_task_validation(tmp_path):
    client, cfg, _s = make_client(tmp_path)
    # 非法目标（注入）
    r = client.post("/api/tasks", json={"name": "evil", "type": "ping", "target": "1.1.1.1; rm -rf /"})
    assert r.status_code == 422
    # curl 无 URL
    r = client.post("/api/tasks", json={"name": "c", "type": "curl", "target": ""})
    assert r.status_code == 422
    # 间隔下限
    r = client.post("/api/tasks", json={"name": "fast", "type": "ping", "target": "1.1.1.1",
                                        "interval_seconds": 3})
    assert r.status_code == 422  # ge=10


def test_admin_token_gate(tmp_path):
    cfg = Config({"server": {"database": str(tmp_path / "t2.db"),
                             "admin_token": "secret123"}})
    client = TestClient(create_app(cfg))
    r = client.post("/api/tasks", json={"name": "x", "type": "ping", "target": "1.1.1.1"})
    assert r.status_code == 403
    r = client.post("/api/tasks", json={"name": "x", "type": "ping", "target": "1.1.1.1"},
                    headers={"X-Admin-Token": "secret123"})
    assert r.status_code == 200
