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


# ---------------- P0 收口：聚合派发 / 失败重投 ----------------

def test_alert_grouping_sends_one_notification(tmp_path, monkeypatch):
    """同一规则同一轮里多个目标异常 → 只发一条聚合通知，但历史仍逐目标留痕。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "grp-node")
    tids = [client.post("/api/tasks", json={
        "name": "grp-" + str(i), "type": "ping", "target": "1.1.1.1"}).json()["id"] for i in range(3)]
    ch = client.post("/api/alerts/channels", json={
        "name": "grp-ch", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()
    calls: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0: (calls.append((title, text)) or (True, "ok")))
    client.post("/api/alerts/rules", json={
        "name": "全任务可用率", "metric": "avail", "op": "lt", "threshold": 0.9,
        "window_seconds": 60, "silence_seconds": 60, "channel_ids": [ch["id"]]})

    now_min = int(time.time()) // 60 * 60
    for tid in tids:                       # 三个任务在同一分钟里都失败
        add_results(client, nid, token, ping_rows(tid, now_min - 60, 6, ok=False))
    reagg(s, now_min - 300, now_min + 60)

    ev = alerting.evaluate(s, now_min)
    assert len(ev) == 3, ev                                  # 三个目标各一条历史
    assert all(e["grouped"] == 3 for e in ev), ev             # 都标记为聚合发送
    assert len(calls) == 1, ("只应发一条聚合通知", calls)      # 关键：没有刷屏
    assert "3 个目标" in calls[0][0] and "3 个目标" in calls[0][1]
    assert client.get("/api/alerts").json()["counts"]["firing"] == 3


def test_notify_retry_outbox(tmp_path, monkeypatch):
    """派发失败 → 进重投队列；成功重投后队列转为 done。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "retry-node")
    tid = client.post("/api/tasks", json={
        "name": "retry-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    ch = client.post("/api/alerts/channels", json={
        "name": "retry-ch", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()

    import gpm.server.notify as notify_mod
    state = {"fail": True, "calls": 0}

    def fake_send(channel, title, text, timeout=8.0):
        state["calls"] += 1
        return (False, "HTTP 500: boom") if state["fail"] else (True, "HTTP 200")

    monkeypatch.setattr(notify_mod, "send", fake_send)
    client.post("/api/alerts/rules", json={
        "name": "重投用例", "metric": "avail", "op": "lt", "threshold": 0.9,
        "window_seconds": 60, "silence_seconds": 3600, "channel_ids": [ch["id"]], "task_id": tid})
    now_min = int(time.time()) // 60 * 60
    add_results(client, nid, token, ping_rows(tid, now_min - 60, 6, ok=False))
    reagg(s, now_min - 300, now_min + 60)

    ev = alerting.evaluate(s, now_min)
    assert ev and ev[0]["delivered"] is False and "boom" in ev[0]["error"]
    ob = client.get("/api/alerts/outbox").json()
    assert ob["counts"]["pending"] == 1, ob
    item = ob["items"][0]
    assert item["attempts"] == 0 and item["next_retry_at"] > now_min
    # 退避未到时不动
    assert alerting.retry_pending(s, now_min + 1) == []
    # 到点且渠道恢复 → 重投成功
    state["fail"] = False
    out = alerting.retry_pending(s, now_min + 61)
    assert out and out[0]["status"] == "done", out
    assert client.get("/api/alerts/outbox").json()["counts"] == {"pending": 0, "done": 1, "failed": 0}
    # 手动立即重投（已送达 → 幂等返回成功）
    r = client.post("/api/alerts/outbox/" + str(item["id"]) + "/retry").json()
    assert r["ok"] is True and "已成功送达" in r["detail"]


# ---------------- P1 收口：巡检推送 / 审计 / 事件详情 ----------------

def test_digest_settings_and_push(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    ch = client.post("/api/alerts/channels", json={
        "name": "dg-ch", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()
    calls: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0: (calls.append((title, text)) or (True, "ok")))

    d = client.get("/api/report/digest/settings").json()
    assert d == {"enabled": False, "interval_hours": 24, "channel_ids": [], "last_ts": 0}
    r = client.put("/api/report/digest/settings", json={
        "enabled": True, "interval_hours": 6, "channel_ids": [ch["id"]]}).json()
    assert r["enabled"] is True and r["interval_hours"] == 6 and r["channel_ids"] == [ch["id"]]
    assert client.put("/api/report/digest/settings", json={
        "channel_ids": ["nope"]}).status_code == 422

    pushed = client.post("/api/report/digest/push", json={"hours": 6}).json()
    assert pushed["channels"] == 1 and pushed["ok"] == 1 and calls
    assert "gpm" in calls[0][0] and "可用率" in calls[0][1]
    assert client.get("/api/report/digest/settings").json()["last_ts"] > 0


def test_audit_records_write_operations(tmp_path, monkeypatch):
    """写接口自动留痕（审计中间件）；读接口不记。"""
    client, cfg, s = make_client(tmp_path)
    tid = client.post("/api/tasks", json={
        "name": "audit-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    client.put("/api/tasks/" + tid, json={"interval_seconds": 20})
    client.get("/api/tasks")                      # 读操作不应留痕
    client.delete("/api/tasks/" + tid)

    d = client.get("/api/audit?limit=50").json()
    assert d["counts"]["total"] >= 3, d["counts"]
    actions = [i["action"] for i in d["items"]]
    assert any("任务" in a for a in actions), actions
    row = d["items"][0]
    for key in ("ts", "time", "who", "action", "target", "target_id", "status", "ip", "ok"):
        assert key in row, (key, row)
    assert row["ok"] in (True, False)
    assert all(i["action"] != "GET /api/tasks" for i in d["items"])
    # 目标过滤（审计面板的「任务/节点/告警」筛选）
    only = client.get("/api/audit?target=" + "任务").json()["items"]
    assert only and all("任务" == i["target"] for i in only), only[:2] if only else []


def test_event_detail_and_ack(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "ev-node")
    tid = client.post("/api/tasks", json={
        "name": "ev-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    now = int(time.time())
    add_results(client, nid, token, ping_rows(tid, now - 300, 8, ok=False, step=10))
    reagg(s, now - 900, now + 60)
    iid = s.incident_open(tid, nid, "", "", now - 300, {"event": "probe_fail", "error_class": "timeout"})

    d = client.get("/api/event/" + str(iid)).json()
    assert d["incident"]["id"] == iid and d["incident"]["task_name"] == "ev-ping"
    assert d["incident"]["kind_label"] in ("探测", "节点侧")
    assert "samples" in d["stats"] and "avail" in d["stats"]
    assert isinstance(d["timeline"], list) and isinstance(d["blast"], list)
    assert "bucket" in d["window"]
    assert client.get("/api/event/999999").status_code == 404

    r = client.post("/api/event/" + str(iid) + "/ack", json={"note": "已知悉，上游抖动"}).json()
    assert r["note"] == "已知悉，上游抖动" and r["acked_at"] > 0
    # 备注不再是空串后，再确认时应保留原备注（传空字符串不覆盖）
    client.post("/api/event/" + str(iid) + "/ack", json={"note": ""})
    assert client.get("/api/event/" + str(iid)).json()["incident"]["note"] == "已知悉，上游抖动"
