"""tests/test_tasks_cache.py —— /api/tasks 进程内 TTL 缓存（欠账-4，TestClient 全链路）。

list_tasks 路由逐任务算 streams + 24h 可用率（逐流 SQL），实测 1.7~2.3s；缓存约定：

1. 同 config_version 且 TTL 内：二次请求命中缓存，重计算只跑一次（用计数器证明）；
2. PUT 修改任务 / 新建任务 → config_version 递增 → 下一次请求立即反映新值；
3. ?fresh=1 绕过缓存（强制重算，且不污染缓存窗口）；
4. tasks_cache_seconds=0：行为与旧版一致（每次请求都重算）；
5. 响应结构一字不变：命中缓存的响应与重算的响应内容一致。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app
from gpm.server.storage import Storage


def make_client(tmp_path, cache_seconds=15):
    db = str(tmp_path / "tasks-cache.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0",
                             "tasks_cache_seconds": cache_seconds}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


def count_recompute(monkeypatch, storage):
    """把 storage.result_streams 包上计数器：list_tasks 逐任务调用它，调用次数即重计算规模。"""
    calls = {"n": 0}
    orig = storage.result_streams

    def counted(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(storage, "result_streams", counted)
    return calls


def _new_task(client, name="ping-cache"):
    r = client.post("/api/tasks", json={
        "name": name, "type": "ping", "target": "223.5.5.5", "interval_seconds": 10})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_same_config_version_hits_cache(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    _new_task(client)
    calls = count_recompute(monkeypatch, s)

    r1 = client.get("/api/tasks")
    assert r1.status_code == 200
    n1 = calls["n"]
    assert n1 > 0, "首次请求应真实重算（result_streams 至少被调用一次）"

    r2 = client.get("/api/tasks")
    assert r2.status_code == 200
    assert calls["n"] == n1, "TTL 内同 config_version 二次请求命中缓存，重计算只跑一次"
    assert r2.json() == r1.json(), "命中缓存返回同一 JSON 结构"

    # ?fresh=1 绕过缓存：强制重算，但**不**回写缓存窗口 —— 紧随其后的普通请求仍命中
    r3 = client.get("/api/tasks?fresh=1")
    assert r3.status_code == 200
    assert calls["n"] > n1, "?fresh=1 应绕过缓存强制重算"
    assert r3.json() == r1.json(), "绕过缓存的重算结果与缓存内容一致（结构一字不变）"
    n3 = calls["n"]
    r4 = client.get("/api/tasks")
    assert r4.status_code == 200 and calls["n"] == n3, "fresh 重算不回写缓存，普通请求仍命中"


def test_config_version_bump_invalidates_cache(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    tid = _new_task(client)
    calls = count_recompute(monkeypatch, s)

    r1 = client.get("/api/tasks")
    assert [t["interval_seconds"] for t in r1.json()] == [10]
    n1 = calls["n"]

    # PUT 修改任务 → config_version 递增 → 缓存立即失效，立刻反映新值
    r = client.put(f"/api/tasks/{tid}", json={"interval_seconds": 20})
    assert r.status_code == 200, r.text
    r2 = client.get("/api/tasks")
    assert [t["interval_seconds"] for t in r2.json()] == [20], "config_version 升后立刻反映新值"
    assert calls["n"] > n1, "config_version 变更触发重计算"

    # 新建任务同理：无需等 TTL 过期
    _new_task(client, "ping-cache-2")
    names = {t["name"] for t in client.get("/api/tasks").json()}
    assert "ping-cache-2" in names, "新建任务后缓存立即失效"


def test_fresh_param_bypasses_cache_without_config_change(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    _new_task(client)
    calls = count_recompute(monkeypatch, s)

    assert client.get("/api/tasks").status_code == 200
    n1 = calls["n"]
    # 绕过缓存重算一次（模拟排障时想看实时口径）
    assert client.get("/api/tasks?fresh=1").status_code == 200
    assert calls["n"] > n1


def test_cache_disabled_matches_legacy_behavior(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path, cache_seconds=0)
    _new_task(client)
    calls = count_recompute(monkeypatch, s)

    r1 = client.get("/api/tasks")
    n1 = calls["n"]
    assert n1 > 0
    r2 = client.get("/api/tasks")
    assert calls["n"] > n1, "tasks_cache_seconds=0 时每次请求都重算（与旧版一致）"
    assert r2.json() == r1.json()


def test_ttl_expiry_triggers_recompute(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path, cache_seconds=1)
    _new_task(client)
    calls = count_recompute(monkeypatch, s)

    assert client.get("/api/tasks").status_code == 200
    n1 = calls["n"]
    time.sleep(1.1)                      # 超过 TTL=1s
    assert client.get("/api/tasks").status_code == 200
    assert calls["n"] > n1, "TTL 过期后重新计算（可用率最多陈旧 TTL 秒）"
