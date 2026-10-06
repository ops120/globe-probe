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

    # 窗口端点在服务端会对齐到聚合桶边界（report.sla 内 t_from//step*step），
    # 因此这里用 1 小时窗口即可稳定覆盖刚写入的两个分钟桶
    d = client.get(f"/api/report/sla?t_from={now - 3600}&t_to={now}").json()
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


# ---------------- 事件折叠：源侧抖动合并 + 展示侧分组 ----------------

def test_incident_flap_merge_and_window(tmp_path):
    """业界「有界合并窗口」：关闭后短时间内再次失败 → 重新打开同一事件，而不是新建。"""
    from gpm.server.incidents import IncidentMachine
    from gpm.server.storage import Storage

    s = Storage(str(tmp_path / "flap.db"))
    m = IncidentMachine(s, fail_threshold=3, recover_threshold=2,
                        flap_window=600, flap_max=21600)
    tid, nid, t0 = "t1", "n1", 1_700_000_000
    for i in range(3):                       # 连续失败 → 开事件
        m.on_result(tid, nid, "", "", "fail", t0 + i * 10, "timeout")
    inc = s.list_incidents(limit=10)
    assert len(inc) == 1 and inc[0]["ended_at"] is None
    for i in range(2):                       # 连续成功 → 关事件
        m.on_result(tid, nid, "", "", "ok", t0 + 100 + i * 10, "")
    inc = s.list_incidents(limit=10)
    assert len(inc) == 1 and inc[0]["ended_at"] is not None and inc[0]["reopen_count"] == 0

    # 600s 内再次失败 → 合并回同一事件（不新增行）
    for i in range(3):
        m.on_result(tid, nid, "", "", "fail", t0 + 300 + i * 10, "timeout")
    inc = s.list_incidents(limit=10)
    assert len(inc) == 1, ("同一目标抖动不应拆成多条事件", inc)
    assert inc[0]["ended_at"] is None and inc[0]["reopen_count"] == 1, inc[0]

    # 关闭后超过窗口再失败 → 才算新事件
    for i in range(2):
        m.on_result(tid, nid, "", "", "ok", t0 + 400 + i * 10, "")
    for i in range(3):
        m.on_result(tid, nid, "", "", "fail", t0 + 2000 + i * 10, "timeout")
    inc = s.list_incidents(limit=10)
    assert len(inc) == 2, ("超过合并窗口应新建事件", len(inc))
    assert s.list_incidents(limit=10)[0]["reopen_count"] == 0


def test_sla_incident_groups_folding(tmp_path):
    """同目标事件在 SLA 报表里折叠成一组，底层每条仍保留（可展开、可点详情）。"""
    client, cfg, s = make_client(tmp_path)
    nid, _ = register(client, cfg, "fold-node")
    tid = client.post("/api/tasks", json={
        "name": "fold-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    t0 = int(time.time()) - 600
    # 同一目标 3 次（第 3 次进行中）
    for i in range(3):
        iid = s.incident_open(tid, nid, "223.5.5.5", "", t0 + i * 100,
                              {"error_class": "timeout", "fail_streak": 3})
        if i < 2:
            s.incident_close(iid, t0 + i * 100 + 60)
    # 另一个目标的 1 次
    s.incident_open(tid, nid, "8.8.8.8", "", t0 + 50, {"error_class": "path_fail"})

    d = client.get(f"/api/report/sla?t_from={t0 - 60}&t_to={t0 + 900}").json()
    inc = d["incidents"]
    assert inc["total"] == 4 and inc["group_count"] == 2, inc
    g = next(x for x in inc["groups"] if x["dns"] == "223.5.5.5")
    assert g["count"] == 3 and len(g["items"]) == 3          # 折叠但一条不少
    assert g["ongoing"] is True and g["streams"] == 1
    assert g["flapping"] is True, g                         # 3 次 / 30 分钟内 → 抖动
    # 两次已恢复各 60s；第三条进行中，按「窗口末」计入（这是既定口径）
    assert g["downtime_seconds"] >= 120, g
    assert g["downtime_seconds"] <= 120 + 900, g
    other = next(x for x in inc["groups"] if x["dns"] == "8.8.8.8")
    assert other["count"] == 1 and other["flapping"] is False
    # 折叠不影响整体统计（total/open/downtime 仍按每条事件算）
    assert inc["open"] == 2


# ---------------- 故障快速定位：告警即诊断 ----------------

def test_alert_notification_diagnosis_template(tmp_path, monkeypatch):
    """firing 通知带固定段落【范围】【初判】【持续】【证据】【链接】；public_url 空 → 无链接行。"""
    client, cfg, s = make_client(tmp_path)
    n1, tok1 = register(client, cfg, "tpl-bj")
    n2, tok2 = register(client, cfg, "tpl-sh")
    tid = client.post("/api/tasks", json={
        "name": "tpl-curl", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    ch = client.post("/api/alerts/channels", json={
        "name": "tpl-ch", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()
    calls: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0:
                        (calls.append((title, text)) or (True, "ok")))
    client.post("/api/alerts/rules", json={
        "name": "tpl-规则", "metric": "avail", "op": "lt", "threshold": 0.9,
        "window_seconds": 60, "silence_seconds": 3600, "channel_ids": [ch["id"]],
        "task_id": tid})
    s.setting_set("public_url", "https://gpm.example.com/")

    now_min = int(time.time()) // 60 * 60
    fail_min = now_min - 60
    add_results(client, n1, tok1, ping_rows(tid, fail_min, 6, ok=False))
    add_results(client, n2, tok2, ping_rows(tid, fail_min, 6, ok=False))
    reagg(s, fail_min - 300, now_min + 60)

    ev = alerting.evaluate(s, now_min)
    assert len(ev) == 1 and ev[0]["kind"] == "firing", ev
    title, text = calls[0]
    assert "【告警】" in title
    # 【范围】：两节点最近一轮全失败 → 全节点失败（2/2 节点）
    assert "【范围】全节点失败" in text and "（2/2 节点）" in text, text
    # 【初判】：error_class=timeout → 网络层 + 建议
    assert "【初判】网络层 — " in text, text
    # 【持续】：episode 起点回退到事件 started_at（第 3 次失败 fail_min+20）→ 不足 1 分钟
    assert "【持续】已持续不足 1 分钟（12 次失败）" in text, text
    # 【证据】：最近一次失败的时间与错误类
    assert "【证据】" in text and "timeout" in text, text
    # 【链接】：public_url 已配置 → 深链行（结尾斜杠被去掉）
    assert f"【链接】https://gpm.example.com/index.html?task={tid}&ts=" in text, text

    # 恢复通知保持向后兼容：不带诊断段落
    add_results(client, n1, tok1, ping_rows(tid, now_min, 6, ok=True, step=5))
    add_results(client, n2, tok2, ping_rows(tid, now_min, 6, ok=True, step=5))
    reagg(s, now_min - 300, now_min + 120)
    calls.clear()
    ev2 = alerting.evaluate(s, now_min + 60)
    assert len(ev2) == 1 and ev2[0]["kind"] == "resolved", ev2
    assert "已恢复" in calls[0][0]
    for tag in ("【范围】", "【初判】", "【持续】", "【证据】", "【链接】"):
        assert tag not in calls[0][1], (tag, calls[0][1])


def test_alert_notification_link_omitted_without_public_url(tmp_path, monkeypatch):
    """public_url 未配置（默认空）→ 通知里没有【链接】段落，其余段落不受影响。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "nolink-node")
    tid = client.post("/api/tasks", json={
        "name": "nolink-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    ch = client.post("/api/alerts/channels", json={
        "name": "nolink-ch", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()
    calls: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0:
                        (calls.append((title, text)) or (True, "ok")))
    client.post("/api/alerts/rules", json={
        "name": "nolink-规则", "metric": "avail", "op": "lt", "threshold": 0.9,
        "window_seconds": 60, "silence_seconds": 3600, "channel_ids": [ch["id"]],
        "task_id": tid})
    assert s.setting_get("public_url", "") == ""

    now_min = int(time.time()) // 60 * 60
    add_results(client, nid, token, ping_rows(tid, now_min - 60, 6, ok=False))
    reagg(s, now_min - 300, now_min + 60)
    ev = alerting.evaluate(s, now_min)
    assert ev and ev[0]["kind"] == "firing"
    text = calls[0][1]
    assert "【链接】" not in text
    # 单节点场景：1/1 失败归「部分节点失败」档并带节点名
    assert "【范围】部分节点失败" in text and "（1/1 节点）：nolink-node" in text, text
    assert "【初判】网络层" in text, text


# ---------------- 故障快速定位：升级链 ----------------

def _escalation_setup(tmp_path, monkeypatch, escalate_minutes):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "esc-node")
    tid = client.post("/api/tasks", json={
        "name": "esc-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    ch = client.post("/api/alerts/channels", json={
        "name": "esc-ch", "type": "webhook", "config": {"url": "https://example.com/h"}}).json()
    calls: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0:
                        (calls.append((title, text)) or (True, "ok")))
    rule = client.post("/api/alerts/rules", json={
        "name": "esc-规则", "metric": "avail", "op": "lt", "threshold": 0.9,
        # 窗口拉长到 1 小时：让 avail 持续低于阈值，告警在升级周期内保持 firing
        "window_seconds": 3600, "silence_seconds": 3600, "channel_ids": [ch["id"]],
        "task_id": tid}).json()
    # API 层（api_web）暂不透传 escalate_minutes → 直接在存储层设置（归属本模块的契约）
    s.update_rule(rule["id"], {"escalate_minutes": escalate_minutes}, int(time.time()))
    now_min = int(time.time()) // 60 * 60
    add_results(client, nid, token, ping_rows(tid, now_min - 60, 6, ok=False))
    reagg(s, now_min - 300, now_min + 60)
    return client, s, calls, tid, nid, token, now_min


def test_alert_escalation_chain_time_progression(tmp_path, monkeypatch):
    """升级链：未到阈值不升 → 到阈值升一次 → 间隔不小于阈值 → ack 后不再升级 → 恢复不升级。"""
    client, s, calls, tid, nid, token, t0 = _escalation_setup(tmp_path, monkeypatch, 10)

    ev = alerting.evaluate(s, t0)                       # 首轮 firing
    assert [e["kind"] for e in ev] == ["firing"]
    assert all(e["kind"] != "escalate" for e in ev)

    assert alerting.evaluate(s, t0 + 540) == []         # 未到 10 分钟：不升级（静默期内也无提醒）
    ev1 = alerting.evaluate(s, t0 + 600)                # 恰好到阈值 → 升级一次
    assert [e["kind"] for e in ev1] == ["escalate"], ev1
    assert ev1[0]["title"].startswith("【升级】") and "10 分钟" in ev1[0]["title"]
    # 升级通知带【持续】（episode 起点 + 窗口失败数）；探测已停 → 60s 内无新一轮 → 【范围】省略
    assert "【持续】已持续 10 分钟（6 次失败）" in calls[-1][1], calls[-1][1]
    assert "【范围】" not in calls[-1][1]
    row = s.alert_recent(limit=5)[0]
    assert row["status"] == "firing" and row["detail"].startswith("escalated_at=")

    assert alerting.evaluate(s, t0 + 900) == []         # 距上次升级 5 分钟 < 阈值：不升级
    ev2 = alerting.evaluate(s, t0 + 1200)               # 再过 10 分钟 → 第二次升级
    assert [e["kind"] for e in ev2] == ["escalate"]
    assert s.alert_last_escalated(next(r["id"] for r in s.list_rules()), tid) == t0 + 1200

    # 确认关联事件 → 不再升级（哪怕告警窗口仍显示异常）
    inc = next(i for i in s.list_incidents(limit=50, open_only=True) if i["task_id"] == tid)
    assert s.incident_ack(inc["id"], t0 + 1250, "oncall", "处理中")
    assert alerting.evaluate(s, t0 + 2400) == []

    # 恢复：事件随 ok 结果关闭 → 无未恢复事件，升级停止；告警窗口滞后期间无动作
    add_results(client, nid, token, ping_rows(tid, t0 + 2400, 60, ok=True, step=5))
    reagg(s, t0 + 2100, t0 + 2900)
    assert alerting.evaluate(s, t0 + 2460) == []
    # 窗口滞后结束（成功率回到阈值以上）→ resolved；之后不再有任何升级
    ev3 = alerting.evaluate(s, t0 + 3200)
    assert [e["kind"] for e in ev3] == ["resolved"], ev3
    assert alerting.evaluate(s, t0 + 3600) == []        # 已恢复：不再升级


def test_alert_escalation_disabled_when_zero(tmp_path, monkeypatch):
    """escalate_minutes=0（默认）→ 永不升级（哪怕持续 2 小时未确认）。"""
    client, s, calls, tid, nid, token, t0 = _escalation_setup(tmp_path, monkeypatch, 0)
    ev = alerting.evaluate(s, t0)
    assert [e["kind"] for e in ev] == ["firing"]
    kinds = [e["kind"] for e in alerting.evaluate(s, t0 + 600)]      # 已过「典型阈值」时段
    assert "escalate" not in kinds and all(k in ("remind", "resolved") for k in kinds), kinds
    kinds2 = [e["kind"] for e in alerting.evaluate(s, t0 + 7200)]
    assert "escalate" not in kinds2
    assert not [c for c in calls if c[0].startswith("【升级】")]


def test_alert_escalation_node_offline(tmp_path, monkeypatch):
    """节点离线告警也走升级链：ack 节点事件后停止。"""
    client, cfg, s = make_client(tmp_path)
    nid, _tok = register(client, cfg, "esc-off-node")
    ch = client.post("/api/alerts/channels", json={
        "name": "esc-off-ch", "type": "webhook",
        "config": {"url": "https://example.com/h"}}).json()
    calls: list = []
    import gpm.server.notify as notify_mod
    monkeypatch.setattr(notify_mod, "send",
                        lambda channel, title, text, timeout=8.0:
                        (calls.append((title, text)) or (True, "ok")))
    rule = client.post("/api/alerts/rules", json={
        "name": "esc-off-规则", "metric": "node_offline", "op": "eq", "threshold": 1,
        "silence_seconds": 3600, "channel_ids": [ch["id"]], "node_id": nid}).json()
    s.update_rule(rule["id"], {"escalate_minutes": 5}, int(time.time()))
    now = int(time.time())
    # 用与线上一致的路径触发离线：心跳过期 → sweep_offline 标记离线并开「节点离线」事件
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (now - 9999, nid))
    s.db.commit()
    assert s.sweep_offline(60, now) == 1

    ev = alerting.evaluate(s, now)
    assert [e["kind"] for e in ev] == ["firing"]
    assert alerting.evaluate(s, now + 240) == []        # 未到 5 分钟
    ev1 = alerting.evaluate(s, now + 300)
    assert [e["kind"] for e in ev1] == ["escalate"], ev1
    assert "【升级】节点" in ev1[0]["title"]
    node_inc = next(i for i in s.list_incidents(limit=10, open_only=True)
                    if i["kind"] == "node" and i["node_id"] == nid)
    s.incident_ack(node_inc["id"], now + 310, "oncall")
    assert alerting.evaluate(s, now + 700) == []        # 已确认：不再升级


# ---------------- 故障快速定位：MTTR 分段 ----------------

def test_sla_mtta_mttr_segments(tmp_path):
    """mtta（触发→首次 ack）/ mttr（ack→恢复）只统计已关闭且已 ack 的探测事件。"""
    from gpm.server import report
    client, cfg, s = make_client(tmp_path)
    register(client, cfg, "mttr-node")
    t0 = int(time.time()) - 7200
    # 样本1：ack 60s，ack→恢复 300s
    i1 = s.incident_open("t1", "n1", "", "", t0, {"error_class": "timeout"})
    s.incident_ack(i1, t0 + 60, "ops")
    s.incident_close(i1, t0 + 360)
    # 样本2：ack 120s，ack→恢复 300s
    i2 = s.incident_open("t1", "n1", "", "", t0 + 600, {"error_class": "timeout"})
    s.incident_ack(i2, t0 + 720, "ops")
    s.incident_close(i2, t0 + 1020)
    # 已恢复但未 ack → 不计入
    i3 = s.incident_open("t1", "n1", "", "", t0 + 1200, {"error_class": "timeout"})
    s.incident_close(i3, t0 + 1300)
    # 进行中且已 ack → 不计入（未恢复）
    i4 = s.incident_open("t1", "n1", "", "", t0 + 1400, {"error_class": "timeout"})
    s.incident_ack(i4, t0 + 1450, "ops")
    # 节点侧事件即使已 ack 已恢复也不计入（硬红线：节点离线 != 目标故障）
    i5 = s.incident_open("", "n1", "", "", t0 + 1500, {"event": "offline"})
    s.incident_ack(i5, t0 + 1520, "ops")
    s.node_incident_close("n1", t0 + 1600)

    d = report.sla(s, t0 - 60, t0 + 1800)
    assert d["mtta"] == {"p50_s": 90.0, "mean_s": 90.0}, d["mtta"]
    assert d["mttr"] == {"p50_s": 300.0, "mean_s": 300.0}, d["mttr"]
    assert d["mtta_note"] == "" and d["mttr_note"] == ""
    assert d["incidents"]["mttr_seconds"] is not None   # 旧的「触发→恢复」口径保留
    # 报表 API 透出
    r = client.get(f"/api/report/sla?t_from={t0 - 60}&t_to={t0 + 1800}").json()
    assert r["mtta"]["p50_s"] == 90.0 and "mtta_note" in r and "mttr_note" in r

    # 样本不足（窗口内没有已确认且已恢复的事件）→ None + note
    d2 = report.sla(s, t0 + 50000, t0 + 60000)
    assert d2["mtta"] == {"p50_s": None, "mean_s": None}
    assert d2["mttr"] == {"p50_s": None, "mean_s": None}
    assert "MTTA 无法计算" in d2["mtta_note"] and "无法计算" in d2["mttr_note"]

# ---------------- 第四轮复核回归钉（2026-10-06）：显式 0 真值门 ----------------

def test_rule_create_keeps_explicit_zero_silence(tmp_path):
    """silence_seconds=0 是合法值（= 每轮都提醒），新建规则必须原样落库。

    复核发现：api_web 声明该字段合法区间含 0 且确实收下，但 storage.create_rule 写的是
    int(fields.get("silence_seconds") or 1800)——显式 0 是假值，被静默换成 30 分钟，
    用户设了却不知道没生效（update_rule 用 is not None 所以改一次就对了，两条路径不一致）。
    变异测试：还原成 or 1800 时本用例必须红。"""
    client, cfg, s = make_client(tmp_path)
    r = client.post("/api/alerts/rules", json={
        "name": "零静默", "metric": "avail", "op": "lt", "threshold": 0.5,
        "window_seconds": 60, "silence_seconds": 0, "channel_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["silence_seconds"] == 0, \
        f"显式 0 必须落库为 0，实际 {r.json()['silence_seconds']}"
    row = next(x for x in s.list_rules() if x["id"] == r.json()["id"])
    assert row["silence_seconds"] == 0, row


def test_rule_create_keeps_default_when_field_absent(tmp_path):
    """反向对照：字段缺失时才用默认值（防把「缺省」也一起改坏）。"""
    client, cfg, s = make_client(tmp_path)
    r = client.post("/api/alerts/rules", json={
        "name": "不传静默", "metric": "avail", "op": "lt", "threshold": 0.5,
        "channel_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["silence_seconds"] == 1800, r.json()

