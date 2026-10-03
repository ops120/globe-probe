"""tests/test_audit_enable.py —— 启停任务显式审计（TestClient 全链路，风格仿 test_pipeline.py）。

PUT /api/tasks/{id} 请求里 enabled 与库内旧值不同且更新成功时，除中间件的通用
「修改任务」外，还应显式落一条「启用任务」/「停用任务」，目标类型/ID 与现有审计一致：

1. 停用（1 -> 0）与启用（0 -> 1）各记一条，target_id 是任务 id，status=200；
2. 请求不带 enabled、或 enabled 与旧值相同：不补记；
3. 中间件的「修改任务」记录保留不动。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app
from gpm.server.storage import Storage


def make_client(tmp_path):
    db = str(tmp_path / "audit-enable.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), storage


def _audit(client, **params):
    return client.get("/api/audit", params={"limit": 200, **params}).json()["items"]


def test_disable_then_enable_recorded_with_chinese_actions(tmp_path):
    client, s = make_client(tmp_path)
    tid = client.post("/api/tasks", json={
        "name": "ping-audit", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]

    # 停用：enabled 1 -> 0
    r = client.put(f"/api/tasks/{tid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] == 0
    # 启用：0 -> 1
    r = client.put(f"/api/tasks/{tid}", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] == 1

    items = _audit(client)
    stops = [i for i in items if i["action"] == "停用任务"]
    starts = [i for i in items if i["action"] == "启用任务"]
    assert len(stops) == 1 and len(starts) == 1, \
        f"应各出现一条 启用/停用任务，实际: {[i['action'] for i in items]}"
    for row in stops + starts:
        assert row["target"] == "任务" and row["target_id"] == tid
        assert row["ok"] and row["status"] == 200
        assert row["who"] == "本机", "未配 admin_token 时操作者应是「本机」"
    # 中间件的通用「修改任务」每条 PUT 各记一条，保留不动
    assert sum(1 for i in items if i["action"] == "修改任务") == 2


def test_no_extra_entry_when_absent_or_unchanged(tmp_path):
    client, s = make_client(tmp_path)
    tid = client.post("/api/tasks", json={
        "name": "ping-audit2", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]

    # 不带 enabled：只是普通修改
    r = client.put(f"/api/tasks/{tid}", json={"interval_seconds": 30})
    assert r.status_code == 200
    # enabled 与旧值（1）相同：不算启停变更
    r = client.put(f"/api/tasks/{tid}", json={"enabled": True})
    assert r.status_code == 200

    items = _audit(client)
    assert not [i for i in items if i["action"] in ("启用任务", "停用任务")], \
        f"未发生启停变化不应补记，实际: {[i['action'] for i in items]}"
    assert sum(1 for i in items if i["action"] == "修改任务") == 2


def test_failed_update_does_not_record(tmp_path):
    client, s = make_client(tmp_path)
    # 任务不存在 -> 404，不应落任何审计
    r = client.put("/api/tasks/t-nope", json={"enabled": False})
    assert r.status_code == 404
    # 422 校验失败同样不该出现启停记录（先建一个合法任务再打非法目标）
    tid = client.post("/api/tasks", json={
        "name": "ping-audit3", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]
    r = client.put(f"/api/tasks/{tid}", json={"target": "not a target!"})
    assert r.status_code == 422

    assert not [i for i in _audit(client) if i["action"] in ("启用任务", "停用任务")]
