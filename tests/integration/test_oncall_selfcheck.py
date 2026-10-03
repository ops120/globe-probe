"""第四期（平台自证）回归：先排除「监控自己坏了」，再谈故障。

- 节点资源饱和度：实测 win-local 长期 CPU 90~95%，这种节点上的失败要先怀疑节点自身。
- 平台可信度三数：探测新鲜度 / 通知渠道可用 / **事件自愈（不可信事件数）**。
  第三个数就是优化文档 §三 那三条自查 SQL —— 正常恒为 0，>0 说明收口逻辑退化了，
  页面要自己报警，而不是让运维去一张张数卡片。
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
    db = str(tmp_path / "sc.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name):
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "version": "0.1.0",
        "system": {}})
    assert r.status_code == 200, r.text
    return r.json()["node_id"], hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def make_task(client, name):
    r = client.post("/api/tasks", json={
        "name": name, "type": "ping", "target": "1.1.1.1", "interval_seconds": 10})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def open_incident(client, nid, token, tid, ts0=None):
    ts0 = ts0 or int(time.time()) - 60
    res = [{"ts": ts0 + i * 10, "task_id": tid, "type": "ping", "dns": "", "url": "",
            "status": "fail", "error_class": "timeout", "metrics": {}} for i in range(3)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})


def selfcheck(client):
    return client.get("/api/oncall").json()["selfcheck"]


def _put(s, task, node, status, ts, error_class="timeout"):
    s.db.execute("INSERT OR REPLACE INTO probe_results(ts,task_id,node_id,type,dns,url,"
                 "status,error_class,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (ts, task, node, "ping", "", "", status, error_class, ts))
    s.db.commit()


# ---------------------------------------------------------------- 结构

def test_selfcheck_shape(tmp_path):
    client, cfg, s = make_client(tmp_path)
    register(client, cfg, "n1")
    sc = selfcheck(client)
    assert set(sc) == {"probe_age_s", "channels", "zombie_events", "zombie_detail"}
    assert sc["zombie_events"] == 0 and sc["zombie_detail"] == {
        "stale_ok": [], "disabled": [], "node_online": []}
    assert set(sc["channels"]) == {"enabled", "up", "unknown", "down", "down_list"}


def test_probe_age_reflects_latest_sample(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    open_incident(client, nid, token, tid, ts0=int(time.time()) - 30)
    age = selfcheck(client)["probe_age_s"]
    assert age is not None and 0 <= age < 120, age


def test_nodes_health_carries_resources(tmp_path):
    """节点条要能看出「这台机器是不是自己快撑不住了」（CPU/内存取最近一次心跳）。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "hot-node")
    now = int(time.time())
    hb = client.post("/api/agent/sync", json={
        "node_id": nid, "token": token, "stats": {"cpu": 93.0, "mem": 88.0, "tasks": 7}})
    assert hb.status_code == 200, hb.text
    nh = client.get("/api/oncall").json()["nodes_health"]
    assert len(nh) == 1
    n = nh[0]
    assert n["node_id"] == nid and n["status"] == "online"
    assert n["cpu"] == 93.0 and n["mem"] == 88.0 and n["streams"] == 7
    assert n["heartbeat_age_s"] is not None and 0 <= n["heartbeat_age_s"] < 60
    _ = now


# ---------------------------------------------------------------- 三条自查各命中一次

def _stale_ok_zombie(s, tid, nid):
    """事件开着，但该流最后一条样本已是 ok；**不走状态机**（状态机会收口）。"""
    for i in range(3):
        _put(s, tid, nid, "fail", int(time.time()) - 300 + i)
    row = s.db.execute(
        "INSERT INTO incidents(task_id,node_id,dns,url,started_at,kind,reason_json)"
        " VALUES(?,?,'','',?, 'probe', '{}')",
        (tid, nid, int(time.time()) - 300))
    s.db.commit()
    return int(row.lastrowid)


def test_detects_stale_ok_zombie(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    iid = _stale_ok_zombie(s, tid, nid)
    _put(s, tid, nid, "ok", int(time.time()) - 10)      # 最后一条是 ok
    z = selfcheck(client)["zombie_detail"]
    assert z["stale_ok"] == [iid], z
    assert selfcheck(client)["zombie_events"] == 1


def test_detects_disabled_task_zombie(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    open_incident(client, nid, token, tid)
    s.db.execute("UPDATE tasks SET enabled=0 WHERE id=?", (tid,))   # 绕过会收口的 update_task
    s.db.commit()
    z = selfcheck(client)["zombie_detail"]
    assert len(z["disabled"]) == 1 and selfcheck(client)["zombie_events"] == 1


def test_detects_node_online_zombie(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, _ = register(client, cfg, "n1")
    now = int(time.time())
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (now - 300, nid))
    s.db.commit()
    s.sweep_offline(60, now)
    s.db.execute("UPDATE nodes SET status='online' WHERE id=?", (nid,))   # 绕过收口
    s.db.commit()
    z = selfcheck(client)["zombie_detail"]
    assert len(z["node_online"]) == 1 and selfcheck(client)["zombie_events"] == 1


def test_clean_instance_reports_zero(tmp_path):
    """正常收口路径下必须恒为 0：这是「第一屏没有说假话」的自证。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    open_incident(client, nid, token, tid)
    s.update_task(tid, {"enabled": False}, int(time.time()))    # 走正常收口
    assert selfcheck(client)["zombie_events"] == 0


# ---------------------------------------------------------------- 渠道口径一致

def test_channel_health_matches_metric_semantics(tmp_path):
    """与 gpm_notify_channel_up 同一口径：只在 enabled 渠道里统计，
    从未自检过单独计 unknown（未知不是 down）。"""
    from gpm.server import metrics
    client, cfg, s = make_client(tmp_path)
    now = int(time.time())
    ch = s.create_channel if hasattr(s, "create_channel") else None
    assert ch, "storage 应有 create_channel"
    ch("c1", "ok", "webhook", {"url": "http://x/"}, now)
    ch("c2", "stale", "webhook", {"url": "http://x/"}, now)
    ch("c3", "off", "webhook", {"url": "http://x/"}, now)
    ch("c4", "never", "webhook", {"url": "http://x/"}, now)
    s.db.execute("UPDATE notify_channels SET last_ok_at=? WHERE id='c1'", (now - 30,))
    s.db.execute("UPDATE notify_channels SET last_ok_at=? WHERE id='c2'", (now - 99999,))
    s.db.execute("UPDATE notify_channels SET last_ok_at=? WHERE id='c3'", (now - 30,))
    s.db.execute("UPDATE notify_channels SET enabled=0 WHERE id='c3'")
    s.db.execute("UPDATE notify_channels SET last_ok_at=0 WHERE id='c4'")
    s.db.commit()

    h = metrics.channel_health(s, now)
    assert h["enabled"] == 3, h          # c3 停用 → 不计入
    assert h["up"] == 1 and h["down"] == 1 and h["unknown"] == 1, h
    assert h["down_list"][0]["id"] == "c2"

    text = metrics.render(s, now_ts=now)
    assert 'gpm_notify_channel_up{channel_id="c1",name="ok"} 1' in text
    assert 'gpm_notify_channel_up{channel_id="c2",name="stale"} 0' in text
    assert 'gpm_notify_channel_up{channel_id="c3",name="off"} 0' in text   # 停用也输出 0
    assert "c4" not in text, "从未自检过的不输出该系列"
