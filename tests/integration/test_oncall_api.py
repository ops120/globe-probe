"""值班页（On-call）后端集成测试：/api/oncall、/api/nodes/capabilities、渠道自检。

对应 ONCALL_OPTIMIZATION.md 第二期 5 / 第三期 10-11；全部走真实 Storage +
TestClient（零 mock 网络：通知发送用注入的假 sender，与 notify.send 同签名）。
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server.app import channel_selfcheck_tick, create_app  # noqa: E402

ONCALL_FIELDS = {"incident_id", "task_id", "task_name", "type", "node_id", "node_name",
                 "dns", "url", "error_class", "layer", "advice", "scope", "started_at",
                 "duration_s", "last_status", "last_ts", "last_age_s", "bucket", "acked"}


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "test.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name, system=None):
    """注册节点并返回 (node_id, token)。"""
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "version": "0.1.0",
        "system": system or {"os": "test"}})
    assert r.status_code == 200, r.text
    return r.json()["node_id"], hashlib.sha256(f"{name}:{reg}".encode()).hexdigest()


def post_results(client, node_id, token, results):
    r = client.post("/api/agent/results", json={"node_id": node_id, "token": token,
                                                "results": results})
    assert r.status_code == 200, r.text


def fail_round(client, node_id, token, tid, ts0, error_class="timeout", n=3):
    """连续 n 次失败（达到 fail_threshold=3 自动开事件）。"""
    post_results(client, node_id, token, [
        {"ts": ts0 + i, "task_id": tid, "type": "ping", "dns": "", "url": "",
         "status": "fail", "error_class": error_class, "metrics": {}}
        for i in range(n)])


# ---------------------------------------------------------------- /api/oncall


def test_oncall_two_nodes_one_fail(tmp_path):
    """2 节点 1 挂：scope=single_node（diagnose.verdict 口径）、layer/advice 正确、
    last_status/last_ts 取事件对应流最近一次、acked 随确认翻转。"""
    client, cfg, s = make_client(tmp_path)
    n1, tk1 = register(client, cfg, "bj-node", {"os": "linux"})
    n2, tk2 = register(client, cfg, "sh-node", {"os": "linux"})
    tid = client.post("/api/tasks", json={
        "name": "ping-oncall", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]
    ts0 = int(time.time()) - 30
    fail_round(client, n1, tk1, tid, ts0)
    post_results(client, n2, tk2, [
        {"ts": ts0 + 3, "task_id": tid, "type": "ping", "dns": "", "url": "",
         "status": "ok", "metrics": {}}])

    r = client.get("/api/oncall")
    assert r.status_code == 200, r.text
    body = r.json()
    # 第二期新增 groups（聚合后的「行动项」）；第三期新增 public_url /
    # public_url_configured（未配置时页面显著提示「通知里的链接未启用」）。
    # items 保持原样以兼容既有前端与断言。
    assert set(body) == {"ts", "items", "groups",
                         "public_url", "public_url_configured"} and body["ts"] > 0
    items = [i for i in body["items"] if i["task_id"] == tid]
    assert len(items) == 1, body["items"]
    it = items[0]
    assert ONCALL_FIELDS <= set(it)
    assert it["task_name"] == "ping-oncall" and it["type"] == "ping"
    assert it["node_id"] == n1 and it["node_name"] == "bj-node"
    assert it["error_class"] == "timeout"
    assert it["layer"] == "网络层", it
    assert "mtr" in it["advice"]
    assert it["scope"]["mode"] == "single_node", it["scope"]
    assert it["scope"]["failed"] == 1 and it["scope"]["total"] == 2
    assert it["scope"]["failed_names"] == ["bj-node"]
    assert it["last_status"] == "fail" and it["last_ts"] == ts0 + 2
    assert it["acked"] is False
    assert it["started_at"] == ts0 + 2 and it["duration_s"] >= 0

    # 确认后 acked=True（事件确认存储字段 acked_at）
    iid = it["incident_id"]
    r2 = client.post(f"/api/event/{iid}/ack", json={"who": "oncall-test"})
    assert r2.status_code == 200, r2.text
    it2 = next(i for i in client.get("/api/oncall").json()["items"]
               if i["incident_id"] == iid)
    assert it2["acked"] is True


# ---------------------------------------------------------------- 分档口径

def test_oncall_bucket_rules():
    """分档规则：「沉默 ≠ 故障」——事件开着不代表此刻还在失败。

    线上实测：11 张值班卡里 9 张是陈旧/失效的（任务停用、节点在线却挂着离线事件、
    连续 ok 却仍开着），第一屏不可信，运维第二次就不看了。
    """
    from gpm.server.api_web import _oncall_bucket

    # 新鲜失败 → 正在失败
    assert _oncall_bucket({"interval_seconds": 60}, {}, "fail", 30, 21600) == "live"
    # 失败样本已过期（但未到陈旧阈值）→ 沉默待确认，不能算「正在失败」
    assert _oncall_bucket({"interval_seconds": 60}, {}, "fail", 2000, 21600) == "silent"
    # 远超陈旧阈值 → 陈旧待收口
    assert _oncall_bucket({"interval_seconds": 60}, {}, "ok", 40 * 3600, 21600) == "stale"
    # 无样本 → 陈旧（收不到样本这件事本身要显式暴露，而不是装作还在坏）
    assert _oncall_bucket({"interval_seconds": 60}, {}, None, None, 21600) == "stale"
    # 节点侧：离线就是真在坏；其余按陈旧
    assert _oncall_bucket({}, {"status": "offline"}, None, None, 21600) == "live"
    assert _oncall_bucket({}, {"status": "online"}, None, None, 21600) == "stale"


def test_oncall_node_event_has_node_layer(tmp_path):
    """节点离线事件没有 error_class，不能因此显示「待定位」。"""
    client, cfg, s = make_client(tmp_path)
    nid, _ = register(client, cfg, "nd-layer", {"os": "linux"})
    ts0 = int(time.time())
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (ts0 - 300, nid))
    s.db.commit()
    s.sweep_offline(60, ts0)

    items = client.get("/api/oncall").json()["items"]
    node_items = [i for i in items if not i["task_id"]]
    assert len(node_items) == 1, items
    it = node_items[0]
    assert it["layer"] == "节点侧", it
    assert "心跳" in it["advice"]
    assert it["bucket"] == "live"                 # 节点确实离线
    assert it["last_status"] is None and it["last_age_s"] is None


def test_oncall_bucket_reported_for_incidents(tmp_path):
    """每个 item 都要带 bucket/last_age_s，前端分档靠它。"""
    client, cfg, s = make_client(tmp_path)
    n1, tk1 = register(client, cfg, "bj-b", {"os": "linux"})
    tid = client.post("/api/tasks", json={
        "name": "ping-bucket", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]
    ts0 = int(time.time()) - 30
    fail_round(client, n1, tk1, tid, ts0)
    it = client.get("/api/oncall").json()["items"][0]
    assert it["bucket"] == "live" and it["last_age_s"] is not None


def test_oncall_scope_partial_all_nodes_and_empty(tmp_path):
    """三档范围结论各命中一次；无 open 事件时 items 为空。"""
    client, cfg, s = make_client(tmp_path)
    nodes = [register(client, cfg, f"scope-node-{i}")[0] for i in range(3)]
    tokens = {nid: hashlib.sha256(
        f"scope-node-{i}:{cfg.agent['register_token']}".encode()).hexdigest()
        for i, nid in enumerate(nodes)}
    tid = client.post("/api/tasks", json={
        "name": "ping-scope", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]
    ts0 = int(time.time()) - 20

    # partial：3 节点挂 2（1 < failed < total）
    fail_round(client, nodes[0], tokens[nodes[0]], tid, ts0)
    fail_round(client, nodes[1], tokens[nodes[1]], tid, ts0)
    post_results(client, nodes[2], tokens[nodes[2]], [
        {"ts": ts0 + 3, "task_id": tid, "type": "ping", "dns": "", "url": "",
         "status": "ok", "metrics": {}}])
    items = [i for i in client.get("/api/oncall").json()["items"] if i["task_id"] == tid]
    assert len(items) == 2 and all(i["scope"]["mode"] == "partial" for i in items), items

    # all_nodes：3/3 全挂（另开一条同错误窗的流凑齐第三节点）
    ts1 = int(time.time()) - 10
    fail_round(client, nodes[0], tokens[nodes[0]], tid, ts1)
    fail_round(client, nodes[1], tokens[nodes[1]], tid, ts1)
    fail_round(client, nodes[2], tokens[nodes[2]], tid, ts1)
    items = [i for i in client.get("/api/oncall").json()["items"] if i["task_id"] == tid]
    assert len(items) == 3
    assert {i["scope"]["mode"] for i in items} == {"all_nodes"}
    assert all(i["scope"]["failed"] == 3 and i["scope"]["total"] == 3 for i in items)

    # 全部恢复 → items 为空（open_only 口径）
    for nid in nodes:
        post_results(client, nid, tokens[nid], [
            {"ts": ts1 + i, "task_id": tid, "type": "ping", "dns": "", "url": "",
             "status": "ok", "metrics": {}} for i in range(2)])
    assert client.get("/api/oncall").json()["items"] == []


def test_oncall_limit_and_node_incident_shape(tmp_path):
    """kind=node（节点离线）事件同样成行：无任务信息，但层面必须落到「节点侧」。

    节点事件没有 error_class，早期实现直接走 classify 兜底成「待定位」——值班的人
    第一眼看到的就是一个没有信息量的『待定位』。"""

    client, cfg, s = make_client(tmp_path)
    nid, _token = register(client, cfg, "lonely-node")
    ts0 = int(time.time()) - 60
    iid = s.node_incident_open(nid, ts0, {"reason": "heartbeat timeout"})
    items = client.get("/api/oncall").json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["incident_id"] == iid and it["node_name"] == "lonely-node"
    assert it["task_id"] == "" and it["task_name"] == ""
    assert it["layer"] == "节点侧" and it["last_status"] is None and it["last_ts"] == 0
    assert "心跳" in it["advice"]
    # 该节点本身是 online（这条事件是直接构造的），所以分档为「陈旧」而不是「正在失败」
    assert it["bucket"] == "stale"
    assert it["scope"]["mode"] == ""   # 没有失败样本，不做范围结论
    # limit 参数生效
    items = client.get("/api/oncall?limit=1").json()["items"]
    assert len(items) == 1


# ---------------------------------------------------- /api/nodes/capabilities


def test_nodes_capabilities_inference(tmp_path):
    """能力矩阵三种推断路径：成功记录→true / tool_missing→false / 无记录→null，
    外加 psutil（心跳 cpu/mem）、ipv6（解析出过 v6）、os（system.os）。"""
    client, cfg, s = make_client(tmp_path)
    n_mtr, tk1 = register(client, cfg, "cap-mtr", {"os": "linux"})
    n_miss, tk2 = register(client, cfg, "cap-miss", {"os": "windows"})
    n_trt, tk3 = register(client, cfg, "cap-tracert", {"os": "windows"})
    n_none, _tk4 = register(client, cfg, "cap-none", {"os": "linux"})
    # 注册即带 last_heartbeat（register_node 写入）→ 抹掉它模拟「从未上报过心跳」
    s.db.execute("UPDATE nodes SET last_heartbeat=0 WHERE id=?", (n_none,))
    s.db.commit()
    tid = client.post("/api/tasks", json={
        "name": "mtr-cap", "type": "mtr", "target": "8.8.8.8",
        "interval_seconds": 60}).json()["id"]
    tid6 = client.post("/api/tasks", json={
        "name": "ping-v6", "type": "ping", "target": "2001:db8::1",
        "interval_seconds": 10}).json()["id"]
    ts0 = int(time.time()) - 2 * 3600   # 2h 前：落在默认 24h 窗口内、1h 窗口外（窗口收窄用例）
    # cap-mtr：mtr 成功记录 + v6 解析实证；cap-miss：tool_missing（且是最新信号）
    post_results(client, n_mtr, tk1, [
        {"ts": ts0, "task_id": tid, "type": "mtr", "dns": "", "url": "", "status": "ok",
         "metrics": {"mode": "mtr", "hops": [{"hop": 1, "host": "8.8.8.8",
                                              "loss_pct": 0.0, "avg": 50.0}]}},
        {"ts": ts0 + 1, "task_id": tid6, "type": "ping", "dns": "", "url": "",
         "status": "ok", "resolved_ip": "2001:db8::1", "metrics": {}}])
    post_results(client, n_miss, tk2, [
        {"ts": ts0, "task_id": tid, "type": "mtr", "dns": "", "url": "",
         "status": "skipped", "error_class": "tool_missing", "error": "mtr 未安装",
         "metrics": {}}])
    # cap-tracert：Windows 降级 tracert 产出过 mode=tracert（mtr 本身无记录）
    post_results(client, n_trt, tk3, [
        {"ts": ts0, "task_id": tid, "type": "mtr", "dns": "", "url": "", "status": "ok",
         "metrics": {"mode": "tracert", "hops": [{"hop": 1, "host": "8.8.8.8",
                                                  "loss_pct": 0.0, "avg": 60.0}]}}])
    # 心跳：cap-mtr 有 cpu/mem（psutil=True）；cap-miss 有心跳但无 cpu/mem（False）
    client.post("/api/agent/sync", json={"node_id": n_mtr, "token": tk1,
                                         "config_version": 0,
                                         "stats": {"cpu": 11.5, "mem": 40.0}})
    client.post("/api/agent/sync", json={"node_id": n_miss, "token": tk2,
                                         "config_version": 0, "stats": {}})

    r = client.get("/api/nodes/capabilities")
    assert r.status_code == 200, r.text
    caps = {c["node_id"]: c for c in r.json()}
    assert set(caps) == {n_mtr, n_miss, n_trt, n_none}
    assert caps[n_mtr] == {"node_id": n_mtr, "name": "cap-mtr", "os": "linux",
                           "mtr": True, "tracert": None, "psutil": True, "ipv6": True}
    assert caps[n_miss]["mtr"] is False and caps[n_miss]["tracert"] is False
    assert caps[n_miss]["psutil"] is False and caps[n_miss]["ipv6"] is None
    assert caps[n_trt]["tracert"] is True and caps[n_trt]["mtr"] is None
    assert caps[n_trt]["os"] == "windows"
    assert caps[n_none] == {"node_id": n_none, "name": "cap-none", "os": "linux",
                            "mtr": None, "tracert": None, "psutil": None, "ipv6": None}

    # 「最新信号优先」：tool_missing 之后又有成功记录 → 覆盖为 true；
    # hours 窗口收窄到没有记录 → 全部回 null
    post_results(client, n_miss, tk2, [
        {"ts": ts0 + 60, "task_id": tid, "type": "mtr", "dns": "", "url": "",
         "status": "ok", "metrics": {"mode": "mtr", "hops": []}}])
    caps = {c["node_id"]: c for c in client.get("/api/nodes/capabilities").json()}
    assert caps[n_miss]["mtr"] is True
    caps = {c["node_id"]: c
            for c in client.get("/api/nodes/capabilities?hours=1").json()}
    assert caps[n_mtr]["mtr"] is None and caps[n_mtr]["ipv6"] is None


# ---------------------------------------------------- 渠道自检（app.channel_selfcheck_tick）


def test_channel_selfcheck_tick(tmp_path):
    """到期判定 / 失败写回 last_error / attempts 节流 / settings 关闭开关。"""
    client, cfg, s = make_client(tmp_path)
    ts0 = int(time.time())
    s.create_channel("ch-ok", "企微", "webhook", {"url": "http://hook/good"}, ts0)
    s.create_channel("ch-bad", "钉钉", "webhook", {"url": "http://hook/bad"}, ts0)
    s.create_channel("ch-off", "已停用", "webhook", {"url": "http://hook/off"}, ts0)
    s.update_channel("ch-off", {"enabled": False}, ts0)

    sent: list[str] = []

    def fake_send(channel, title, text, timeout=5.0):
        sent.append(str(channel.get("url")))
        assert title == "【自检】通知渠道探活"
        good = "good" in str(channel.get("url"))
        return (good, "发送成功" if good else "boom: connection refused")

    attempts: dict = {}
    ts = 1_700_000_000
    r1 = channel_selfcheck_tick(s, ts=ts, sender=fake_send, attempts=attempts)
    # last_ok_at=0 → 全部到期；停用渠道跳过
    assert {r["channel_id"] for r in r1} == {"ch-ok", "ch-bad"}
    assert set(sent) == {"http://hook/good", "http://hook/bad"}
    by_id = {r["channel_id"]: r for r in r1}
    assert by_id["ch-ok"]["ok"] is True and by_id["ch-bad"]["ok"] is False
    assert "boom" in by_id["ch-bad"]["detail"]
    c_ok = next(c for c in s.list_channels() if c["id"] == "ch-ok")
    c_bad = next(c for c in s.list_channels() if c["id"] == "ch-bad")
    assert c_ok["last_ok_at"] == ts and c_ok["last_error"] == ""
    assert c_bad["last_ok_at"] == 0 and "boom" in c_bad["last_error"]

    # 同一周期内不重复发（attempts 节流，失败渠道也不会每轮轰炸）
    assert channel_selfcheck_tick(s, ts=ts + 60, sender=fake_send, attempts=attempts) == []
    assert len(sent) == 2

    # 周期到期后再次探测；settings 置 0 → 功能关闭，直接返回
    r3 = channel_selfcheck_tick(s, ts=ts + 700, sender=fake_send, attempts=attempts)
    assert {r["channel_id"] for r in r3} == {"ch-ok", "ch-bad"}
    assert len(sent) == 4
    s.setting_set("channel_selfcheck_minutes", "0")
    assert channel_selfcheck_tick(s, ts=ts + 2000, sender=fake_send,
                                  attempts=attempts) == []
    assert len(sent) == 4


def test_channel_selfcheck_sender_exception_and_due_window(tmp_path):
    """sender 抛异常按失败处理；last_ok_at 推进后渠道在周期内保持安静。"""
    client, cfg, s = make_client(tmp_path)
    ts0 = int(time.time())
    s.create_channel("ch-x", "飞书", "webhook", {"url": "http://hook/x"}, ts0)

    def bad_send(channel, title, text, timeout=5.0):
        raise RuntimeError("network down")

    attempts: dict = {}
    ts = 1_700_000_000
    r1 = channel_selfcheck_tick(s, ts=ts, sender=bad_send, attempts=attempts)
    assert len(r1) == 1 and r1[0]["ok"] is False and "network down" in r1[0]["detail"]
    assert "network down" in next(c for c in s.list_channels()
                                  if c["id"] == "ch-x")["last_error"]
    # 即使整轮 sender 都不可用，下一次 tick（周期内）也不会再碰渠道
    assert channel_selfcheck_tick(s, ts=ts + 30, sender=bad_send, attempts=attempts) == []
    # 自检成功后 last_ok_at 推进 → 周期内安静
    s.channel_touch("ch-x", True, "", ts)
    assert channel_selfcheck_tick(s, ts=ts + 30, sender=None, attempts=attempts) == []
    # 周期一过且 sender=None（真实 notify.send）→ 到期渠道会尝试真实发送。
    # 这里只断言「进入发送路径」：给一个可控 sender。
    r4 = channel_selfcheck_tick(s, ts=ts + 700, sender=fake_send_ok, attempts=attempts)
    assert r4 and r4[0]["ok"] is True


def fake_send_ok(channel, title, text, timeout=5.0):
    return True, "ok"
