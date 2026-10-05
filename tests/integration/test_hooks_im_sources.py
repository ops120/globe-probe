"""IM/通用告警源接入回归（第八期）：钉钉机器人回调、Teams 转发、generic 兜底，
以及「外部告警 + 本地事件 → /api/correlation 关联簇」的端到端链路。

接入口径与四家 webhook 完全一致：必须配置该来源 Token（未配置一律 401）、
限流、幂等去重（同 source_id 覆盖）、落库前脱敏、解析认不出就 0 条不编造。
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server import hooks  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "im.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    hooks.rate_reset()
    return TestClient(create_app(cfg, storage)), cfg, storage


def post_hook(client, source, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers[hooks.TOKEN_HEADER] = token
    return client.post("/api/hooks/%s" % source, json=body, headers=headers)


# ---------------------------------------------------------------- 解析单元

def test_dingtalk_text_firing_and_recovery_share_id():
    """钉钉同一故障的「告警」与「恢复」消息 → 同一 source_id（幂等覆盖而非裂成两条）。"""
    firing = hooks.parse_dingtalk({"msgtype": "text", "text": {
        "content": "【P1】api.example.com 可用率跌破 95%，请尽快处理（2026-10-05 10:00）"},
        "senderNick": "值班群", "timestamp": 1791000000000})
    recover = hooks.parse_dingtalk({"msgtype": "text", "text": {
        "content": "【P1】api.example.com 可用率跌破 95%，已恢复（2026-10-05 10:20）"},
        "senderNick": "值班群", "timestamp": 1791001200000})
    assert len(firing) == 1 and len(recover) == 1
    assert firing[0]["status"] == "firing" and recover[0]["status"] == "resolved"
    assert firing[0]["source_id"] == recover[0]["source_id"]
    assert firing[0]["labels"]["dingtalk_senderNick"] == "值班群"
    assert "api.example.com" in firing[0]["title"]


def test_dingtalk_markdown_shape():
    out = hooks.parse_dingtalk({"msgtype": "markdown", "markdown": {
        "title": "数据库主从延迟", "text": "### 主从延迟\n- 集群 order-db 延迟 300 秒，故障未恢复"},
        "timestamp": "1791000000000"})
    assert len(out) == 1 and out[0]["status"] == "firing"
    assert "主从延迟" in out[0]["title"]


def test_dingtalk_empty_content_is_zero_not_invented():
    assert hooks.parse_dingtalk({"msgtype": "text", "text": {"content": ""}}) == []
    assert hooks.parse_dingtalk({"foo": "bar"}) == []


def test_not_recovered_is_firing():
    """「未/没有/无法恢复」是 firing——不把故障装成已恢复（复合核实的 P1）。"""
    for txt in ("故障未恢复", "故障没有恢复", "服务无法恢复", "尚未恢复",
                "not recovered yet", "still not resolved"):
        out = hooks.parse_dingtalk({"msgtype": "text", "text": {"content": txt}})
        assert out and out[0]["status"] == "firing", (txt, out)
    for txt in ("已恢复", "已修复", "问题解决", "服务正常", "resolved"):
        out = hooks.parse_dingtalk({"msgtype": "text", "text": {"content": txt}})
        assert out and out[0]["status"] == "resolved", (txt, out)


def test_fingerprint_does_not_merge_different_faults():
    """误并反例钉：类型词（异常/失败）不剥、业务单号不剥——不同故障不得共用 id。"""
    a = hooks.parse_dingtalk({"msgtype": "text", "text": {"content": "数据库连接异常"}})
    b = hooks.parse_dingtalk({"msgtype": "text", "text": {"content": "数据库连接失败"}})
    assert a[0]["source_id"] != b[0]["source_id"]
    c = hooks.parse_dingtalk({"msgtype": "text", "text": {"content": "订单202501011234失败"}})
    d = hooks.parse_dingtalk({"msgtype": "text", "text": {"content": "订单202601019999失败"}})
    assert c[0]["source_id"] != d[0]["source_id"]


def test_teams_message_card_and_adaptive_card():
    card = hooks.parse_teams({"@type": "MessageCard", "title": "Azure Monitor 告警",
                              "text": "CPU > 90% 持续 10 分钟，故障"})
    assert len(card) == 1 and card[0]["status"] == "firing" and "Azure" in card[0]["title"]
    adaptive = hooks.parse_teams({"type": "message",
                                  "conversation": {"name": "告警群"},
                                  "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                   "content": {"type": "AdaptiveCard", "body": [
                                                       {"type": "TextBlock", "text": "磁盘空间不足"},
                                                       {"type": "TextBlock", "text": "web-01 已恢复"}]}}]})
    assert len(adaptive) == 1
    assert adaptive[0]["status"] == "resolved" and adaptive[0]["labels"]["teams_channel"] == "告警群"


def test_generic_structured_fields_win_over_text_guess():
    out = hooks.parse_generic({"title": "迁移演练", "status": "RESOLVED", "severity": "P3",
                               "startsAt": 1791000000, "fingerprint": "fp-1",
                               "labels": {"env": "staging", "host": "web-01"}})
    assert out[0]["status"] == "resolved" and out[0]["severity"] == "P3"
    assert out[0]["started_at"] == 1791000000
    assert out[0]["labels"]["env"] == "staging"
    # 无 fingerprint → 内容指纹兜底（同内容同 id，幂等去重仍成立）
    a = hooks.parse_generic({"title": "无 id 的告警", "text": "x"})
    b = hooks.parse_generic({"title": "无 id 的告警", "text": "x"})
    assert a[0]["source_id"] == b[0]["source_id"] and a[0]["source_id"]


# ---------------------------------------------------------------- 接口链路

def test_dingtalk_requires_token_and_dedups(tmp_path):
    client, cfg, s = make_client(tmp_path)
    # 未配置该来源 Token → 401（不提供无鉴权写默认）
    assert client.post("/api/hooks/dingtalk", json={"msgtype": "text", "text": {"content": "x"}}).status_code == 401
    r = client.put("/api/external/settings", json={"hook_token_dingtalk": "tk-d"})
    assert r.status_code == 200 and r.json()["sources"][4]["source"] == "dingtalk"
    assert r.json()["sources"][4]["configured"] is True

    body = {"msgtype": "text", "text": {"content": "【P1】api.example.com 故障"}, "senderNick": "值班群"}
    r1 = post_hook(client, "dingtalk", body, token="tk-d")
    assert r1.status_code == 200 and r1.json()["received"] == 1, r1.text
    r2 = post_hook(client, "dingtalk", body, token="tk-d")
    assert r2.status_code == 200
    items = client.get("/api/external/alerts?source=dingtalk").json()["items"]
    assert len(items) == 1, "同内容重复推送必须被幂等去重"
    assert items[0]["status"] == "firing"
    # 错误 token → 401
    assert post_hook(client, "dingtalk", body, token="wrong").status_code == 401
    # sources 列表反映 7 家
    srcs = [x["source"] for x in client.get("/api/external/settings").json()["sources"]]
    assert srcs == list(hooks.SOURCES) and "teams" in srcs and "generic" in srcs


def test_teams_and_generic_end_to_end(tmp_path):
    client, cfg, s = make_client(tmp_path)
    client.put("/api/external/settings", json={"hook_token": "tk-all"})
    r = post_hook(client, "teams", {"@type": "MessageCard", "title": "订单服务 5xx",
                                    "text": "错误率 30%，故障"}, token="tk-all")
    assert r.status_code == 200 and r.json()["received"] == 1
    r = post_hook(client, "generic", {"title": "缓存命中率下降", "status": "firing",
                                      "startsAt": int(time.time()) - 60}, token="tk-all")
    assert r.status_code == 200
    items = client.get("/api/external/alerts").json()["items"]
    assert {i["source"] for i in items} == {"teams", "generic"}


def test_correlation_endpoint_links_external_to_local(tmp_path):
    """端到端：本地事件 + 带任务名的钉钉告警 → 关联分析把它们连成一簇。"""
    client, cfg, s = make_client(tmp_path)
    client.put("/api/external/settings", json={"hook_token_dingtalk": "tk-d"})
    reg = cfg.agent["register_token"]
    nid = client.post("/api/agent/register", json={
        "name": "node-a", "register_token": reg, "version": "0.1.0",
        "system": {}}).json()["node_id"]
    token = hashlib.sha256(f"node-a:{reg}".encode()).hexdigest()
    tid = client.post("/api/tasks", json={
        "name": "corr-e2e-ping", "type": "ping", "target": "223.5.5.5",
        "interval_seconds": 10}).json()["id"]
    now = int(time.time())
    fails = [{"ts": now - 30 + i * 10, "task_id": tid, "type": "ping", "dns": "", "url": "",
              "status": "fail", "error_class": "timeout", "metrics": {}} for i in range(3)]
    assert client.post("/api/agent/results", json={
        "node_id": nid, "token": token, "results": fails}).status_code == 200

    # 钉钉告警标题带任务名 → 存量关联器按名字+时间窗把它挂到本地事件作旁证
    r = post_hook(client, "dingtalk", {"msgtype": "text", "text": {
        "content": "【P1】corr-e2e-ping 探测失败，请处理"}}, token="tk-d")
    assert r.status_code == 200

    rep = client.get("/api/correlation?hours=1").json()
    assert rep["stats"]["faults"] >= 2, rep["stats"]
    linked = [i for i in client.get("/api/external/alerts?source=dingtalk").json()["items"]
              if i.get("linked_incident")]
    assert linked, "存量关联器应按任务名把外部告警挂到本地事件"
    assert rep["stats"]["clusters"] >= 1
    c = rep["clusters"][0]
    assert any(d["dim"] == "link" for d in c["dims"]), c["dims"]
    assert "旁证" in c["hypothesis"]
    # 窗口裁剪：hours 上限不炸
    assert client.get("/api/correlation?hours=99999").status_code == 200
