"""写口鉴权安全基线（P0 回归）：admin_token 与监听地址的组合矩阵。

曾经 check_write 在 admin_token 未配置时 fail-open——配合示例配置的 0.0.0.0
监听，局域网内任何人都能建任务/导入配置/改通知渠道。现在的约定：

1. 配了 admin_token：写接口必须带正确的 X-Admin-Token（常量时间比较）；
2. 没配 token + 环回监听（默认/本机开发）：放行，保持零配置开发体验；
3. 没配 token + 非环回监听（示例/Docker 部署形态）：一律 403，绝不静默裸奔。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app


def make_client(tmp_path, listen="127.0.0.1:0", admin_token=None):
    from gpm.server.storage import Storage
    srv = {"database": str(tmp_path / "auth.db"), "listen": listen}
    if admin_token is not None:
        srv["admin_token"] = admin_token
    cfg = Config({"server": srv})
    storage = Storage(str(tmp_path / "auth.db"))
    return TestClient(create_app(cfg, storage)), cfg


def test_loopback_without_token_allows_writes(tmp_path):
    """本机开发模式：环回监听 + 未配 token → 写接口直开（历史行为，不破坏开发流）。"""
    client, _ = make_client(tmp_path)
    r = client.post("/api/tasks", json={
        "name": "dev", "type": "ping", "target": "223.5.5.5", "interval_seconds": 30})
    assert r.status_code == 200, r.text


def test_non_loopback_without_token_rejects_writes(tmp_path):
    """fail-closed：非环回监听 + 未配 token → 写接口一律 403（不再静默裸奔）。"""
    client, _ = make_client(tmp_path, listen="0.0.0.0:8620")
    r = client.post("/api/tasks", json={
        "name": "evil", "type": "ping", "target": "223.5.5.5", "interval_seconds": 30})
    assert r.status_code == 403, r.text
    r = client.delete("/api/tasks/whatever")
    assert r.status_code == 403, r.text
    r = client.put("/api/external/settings", json={})
    assert r.status_code == 403, r.text
    # 读接口不受影响（内网信任模型只收敛写口）
    assert client.get("/api/tasks").status_code == 200


def test_non_loopback_with_token_requires_header(tmp_path):
    """配了 token：正确的 X-Admin-Token 放行；错误/缺失 403（即便来自环回）。"""
    client, _ = make_client(tmp_path, listen="0.0.0.0:8620", admin_token="s3cret")
    body = {"name": "t1", "type": "ping", "target": "223.5.5.5", "interval_seconds": 30}
    assert client.post("/api/tasks", json=body).status_code == 403
    assert client.post("/api/tasks", json=body,
                       headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert client.post("/api/tasks", json=body,
                       headers={"X-Admin-Token": "s3cret"}).status_code == 200
