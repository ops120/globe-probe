"""集成测试：告警闭环（渠道/规则/静默/恢复/维护窗口）、SLA 报表、Prometheus 指标。"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server import alerting
from gpm.server.app import create_app


def make_client(tmp_path):
    from gpm.server.storage import Storage
    cfg = Config({"server": {"database": str(tmp_path / "al.db"), "listen": "127.0.0.1:0"}})
    storage = Storage(str(tmp_path / "al.db"))
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name):
    reg = cfg.agent["register_token"]
    nid = client.post("/api/agent/register", json={
        "name": name, "register_token": reg}).json()["node_id"]
    return nid, hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def add_results(client, nid, token, rows):
    r = client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": rows})
    assert r.status_code == 200, r.text
    return r.json()


def reagg(s, t_from, t_to):
    for bkt in ("1m", "5m", "1h"):          # 聚合有层级：必须按序
        s.agg_recompute(bkt, t_from, t_to)


def ping_rows(tid, base, n, ok=True, step=10):
    return [{"ts": base + i * step, "task_id": tid, "type": "ping", "status": "ok" if ok else "fail",
             "error_class": "" if ok else "timeout",
             "metrics": {"rtt_avg": 10.0 if ok else None, "loss_rate": 0.0 if ok else 1.0}}
            for i in range(n)]


# ---------------- 告警闭环 ----------------

def test_alert_fire_silence_resolve(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "alert-node")
    tid = client.post("/api/tasks", json={
        "name": "ping-alert", "type": "ping", "target": "1.1.1.1"}).json()["id"]

    # 渠道（拦住真实发送）
    ch = client.post("/api/alerts/channels", json={
        "name": "运维群", "type": "webhook",
        "config": {"url": "https://example.com/hook"}}).json()
    sent: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0: (sent.append((title, text)) or (True, "ok")))

    rule = client.post("/api/alerts/rules", json={
        "name": "可用率跌破 90%", "metric": "avail", "op": "lt", "threshold": 0.9,
        "window_seconds": 60, "silence_seconds": 1800, "severity": "critical",
        "channel_ids": [ch["id"]], "task_id": tid}).json()
    assert rule["metric"] == "avail" and rule["channel_ids"] == [ch["id"]]

    now_min = int(time.time()) // 60 * 60
    m1, m2 = now_min - 120, now_min          # 前 2 分钟造失败，当前分钟造成功
    add_results(client, nid, token, ping_rows(tid, m1, 6, ok=False))
    add_results(client, nid, token, ping_rows(tid, m2, 6, ok=True, step=5))
    reagg(s, m1 - 300, m2 + 60)

    # 1) 命中 → firing + 已送达
    ev = alerting.evaluate(s, m1 + 30)
    assert len(ev) == 1 and ev[0]["kind"] == "firing" and ev[0]["delivered"] is True, ev
    assert sent and "可用率" in sent[0][0] and "规则" in sent[0][1]
    assert client.get("/api/alerts").json()["counts"]["firing"] == 1
    # 2) 静默期内不重复打扰
    assert alerting.evaluate(s, m1 + 40) == []
    # 3) 条件消失 → resolved，且未恢复计数归零
    ev2 = alerting.evaluate(s, m2 + 30)
    assert len(ev2) == 1 and ev2[0]["kind"] == "resolved", ev2
    assert client.get("/api/alerts?status=firing").json()["counts"]["firing"] == 0
    assert len(sent) == 2 and "已恢复" in sent[1][0]


def test_alert_node_offline_and_maintenance(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    nid, _tok = register(client, cfg, "nodeoff")
    ch = client.post("/api/alerts/channels", json={
        "name": "wh", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send", lambda *a, **k: (True, "ok"))

    client.post("/api/alerts/rules", json={
        "name": "节点离线", "metric": "node_offline", "op": "eq", "threshold": 1,
        "silence_seconds": 60, "channel_ids": [ch["id"]], "node_id": nid})

    now = int(time.time())
    # 维护窗口生效中 → 不告警
    w = client.post("/api/alerts/windows", json={
        "name": "割接", "starts_at": now - 60, "ends_at": now + 600, "node_id": nid}).json()
    s.db.execute("UPDATE nodes SET status='offline', last_heartbeat=? WHERE id=?", (now - 9999, nid))
    s.db.commit()
    assert alerting.evaluate(s, now) == [], "维护窗口内不应告警"
    # 删掉窗口 → 命中离线
    assert client.request("DELETE", f"/api/alerts/windows/{w['id']}").status_code == 200
    ev = alerting.evaluate(s, now)
    assert len(ev) == 1 and "离线" in ev[0]["title"], ev
    # 规则/窗口的校验
    assert client.post("/api/alerts/rules", json={
        "name": "x", "metric": "bogus", "op": "lt", "threshold": 1}).status_code == 422
    assert client.post("/api/alerts/rules", json={
        "name": "y", "metric": "avail", "op": "lt", "threshold": 1,
        "channel_ids": ["nope"]}).status_code == 422      # 引用不存在的渠道 → 422
    assert client.post("/api/alerts/windows", json={
        "starts_at": now, "ends_at": now - 1}).status_code == 422


# ---------------- SLA 报表 ----------------

def test_sla_report_and_digest(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "sla-node")
    tid = client.post("/api/tasks", json={
        "name": "ping-sla", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    now = int(time.time())
    add_results(client, nid, token, ping_rows(tid, now - 600, 10, ok=True))
    add_results(client, nid, token, ping_rows(tid, now - 300, 10, ok=False, step=15))
    reagg(s, now - 3600, now + 60)

    # 窗口要对齐整点：1h 聚合桶的时间戳是整点，若 t_from 落在整点之后，
    # 当前小时的桶会被排除（会随时钟飘红，与实现无关）
    d = client.get(f"/api/report/sla?t_from={now - 3600}&t_to={now + 60}").json()
    assert set(["window", "overall", "tasks", "nodes", "incidents"]).issubset(d)
    assert d["overall"]["count"] >= 20
    assert 0 <= d["overall"]["avail"] < 1
    task = next(t for t in d["tasks"] if t["task_id"] == tid)
    assert task["count"] == d["overall"]["count"] and task["fail"] > 0
    node = next(n for n in d["nodes"] if n["node_id"] == nid)
    assert node["count"] == d["overall"]["count"]
    assert d["incidents"]["total"] >= 0 and "mttr_seconds" in d["incidents"]

    daily = client.get(f"/api/report/daily?task_id={tid}&days=7").json()["items"]
    assert len(daily) == 7 and all("day" in x for x in daily)
    dg = client.get("/api/report/digest?hours=1").json()
    assert "gpm" in dg["title"] and "可用率" in dg["text"]


# ---------------- Prometheus 指标 ----------------

def test_metrics_endpoint(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, _ = register(client, cfg, "metrics-node")
    client.post("/api/tasks", json={"name": "ping-m", "type": "ping", "target": "1.1.1.1"})
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    for line in ("gpm_up", "gpm_results_total", "gpm_nodes_total", "gpm_tasks_total",
                 "gpm_node_up{node=\"metrics-node\"}", "gpm_incidents_open"):
        assert line in body, line
    assert body.endswith("\n")
