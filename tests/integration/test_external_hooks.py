"""第三方告警接入（第六期 24/25/27/28/29/30）回归。

现场问题：接了 Grafana/Zabbix/腾讯云/GCP 之后，如果**不做关联去重**，同一故障会以
「本地一条 + 外部一条」出现，第一屏重新被塞满 —— 接第三方反而让监控更难用。

覆盖：四家 payload 解析、落库前脱敏、接入 Token 鉴权（未配置一律拒绝）、限流、
payload 上限、幂等去重、关联去重（能折进本地卡的就不单独成卡）、按来源汇总、
以及「不产生任何对外请求」这条硬约束。
"""
import base64
import hashlib
import json
import socket
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
    db = str(tmp_path / "ext.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    hooks.rate_reset()
    return TestClient(create_app(cfg, storage)), cfg, storage


def set_token(client, token, source=None):
    body = {"hook_token": token}
    if source:
        body = {"hook_token_%s" % source: token}
    r = client.put("/api/external/settings", json=body)
    assert r.status_code == 200, r.text


def post_hook(client, source, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers[hooks.TOKEN_HEADER] = token
    return client.post("/api/hooks/%s" % source, json=body, headers=headers)


def register(client, cfg, name):
    reg = cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg, "version": "0.1.0", "system": {}})
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


# ---------------------------------------------------------------- 解析（四家）

def test_parse_grafana_alertmanager_webhook():
    body = {"version": "4", "status": "firing", "externalURL": "https://grafana.example.com/",
            "alerts": [
                {"status": "firing", "fingerprint": "fp1",
                 "labels": {"alertname": "HighCPU", "instance": "web01", "severity": "critical"},
                 "annotations": {"summary": "CPU 持续 92%"},
                 "startsAt": "2026-10-03T10:00:00Z",
                 "generatorURL": "https://grafana.example.com/alert/1"},
                {"status": "resolved", "fingerprint": "fp2",
                 "labels": {"alertname": "PingLoss", "instance": "web02"},
                 "annotations": {"summary": "丢包恢复"},
                 "startsAt": "2026-10-03T09:00:00Z", "endsAt": "2026-10-03T09:30:00Z"},
            ]}
    out = hooks.parse("grafana", body)
    assert len(out) == 2
    a = out[0]
    assert a["source_id"] == "fp1" and a["title"] == "CPU 持续 92%"
    assert a["severity"] == "critical" and a["status"] == "firing"
    assert a["started_at"] > 0 and a["labels"]["instance"] == "web01"
    assert out[1]["status"] == "resolved" and out[1]["ended_at"] > 0


def test_rfc3339_is_parsed_as_utc():
    """带 Z 的时间必须按 UTC 解析：当成本地时间会让整条告警偏一个时区（实测差 8 小时）。"""
    import datetime
    want = int(datetime.datetime(2026, 10, 3, 10, 0, 0,
                                 tzinfo=datetime.timezone.utc).timestamp())
    assert hooks._ts_from_any("2026-10-03T10:00:00Z") == want
    assert hooks._ts_from_any("2026-10-03T10:00:00+00:00") == want
    assert hooks._ts_from_any("2026-10-03T18:00:00+08:00") == want
    # 毫秒/秒/裸整数
    assert hooks._ts_from_any(1791000000) == 1791000000
    assert hooks._ts_from_any(1791000000000) == 1791000000
    # 认不出就返回 0，不猜「现在」
    assert hooks._ts_from_any("not a time") == 0
    assert hooks._ts_from_any(None) == 0


def test_parse_zabbix_minimal_json():
    out = hooks.parse("zabbix", {"event_id": "42", "host": "db01", "trigger": "Disk full",
                                 "severity": "High", "status": "PROBLEM", "clock": 1791000000})
    assert len(out) == 1 and out[0]["source_id"] == "42"
    assert out[0]["status"] == "firing" and out[0]["labels"]["host"] == "db01"
    ok = hooks.parse("zabbix", {"event_id": "42", "trigger": "Disk full", "status": "OK"})
    assert ok[0]["status"] == "resolved"
    assert hooks.parse("zabbix", {"host": "no-id"}) == []      # 没有唯一 id 不收


def test_parse_tencent_alarm_callback():
    out = hooks.parse("tencent", {
        "alarmId": "alarm-abc", "alarmName": "CPU 使用率过高", "alarmStatus": "1",
        "alarmTime": 1791000000000, "namespace": "qce/cvm", "metricName": "cpu_usage",
        "instanceObject": {"instanceId": "ins-1", "instanceName": "cvm-web"}})
    assert len(out) == 1 and out[0]["source_id"] == "alarm-abc"
    assert out[0]["status"] == "firing" and out[0]["labels"]["instance"] == "cvm-web"
    ok = hooks.parse("tencent", {"alarmId": "a2", "alarmStatus": "0", "alarmName": "x"})
    assert ok[0]["status"] == "resolved"


def test_parse_gcp_pubsub_push_and_direct():
    inc = {"incident": {"incident_id": "inc-9", "policy_name": "VM CPU",
                        "state": "open", "started_at": 1791000000,
                        "resource": {"type": "gce_instance", "name": "vm-1"},
                        "metric": {"type": "compute.googleapis.com/instance/cpu/utilization"},
                        "summary": "CPU > 90%"}}
    wrapped = {"message": {"data": base64.b64encode(json.dumps(inc).encode()).decode(),
                           "messageId": "m1"}}
    out = hooks.parse("gcp", wrapped)
    assert len(out) == 1 and out[0]["source_id"] == "inc-9"
    assert out[0]["title"] == "CPU > 90%" and out[0]["labels"]["resource"] == "vm-1"
    assert hooks.parse("gcp", inc)[0]["source_id"] == "inc-9"      # 直接给 incident 也认
    assert hooks.parse("gcp", {"message": {"data": "not-base64!!"}}) == []


def test_parse_unknown_source_and_garbage():
    assert hooks.parse("nope", {"a": 1}) == []
    assert hooks.parse("grafana", {"alerts": "not-a-list"}) == []
    assert hooks.parse("grafana", {"alerts": [{"labels": {}}]}) == []   # 无 fingerprint/alertname


# ---------------------------------------------------------------- 脱敏

def test_redact_sensitive_keys_and_urls():
    payload = {"token": "super-secret", "nested": {"api_key": "k", "ok": "keep"},
               "hooks": [{"webhook_url": "https://hooks.example.com/x?token=abc&a=1"}],
               "url": "https://user:pw@example.com/path?token=zzz&keep=1"}
    out = hooks.redact(payload)
    # ① 敏感键名 → 整值替换（webhook_url 整条没了，连带里面的 token 也不再暴露）
    assert out["token"] == "***" and out["hooks"][0]["webhook_url"] == "***"
    assert out["nested"]["api_key"] == "***" and out["nested"]["ok"] == "keep"
    # ② 非敏感键下的 URL 仍要清洗：敏感查询参数抹掉、userinfo 抹掉、无害参数保留
    assert out["url"] == "https://***@example.com/path?token=***&keep=1", out["url"]
    for leak in ("super-secret", "abc", "zzz", "user:pw@"):
        assert leak not in json.dumps(out), leak
    assert hooks.redact({"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}) is not None


def test_redact_caps_list_and_string_length():
    out = hooks.redact({"xs": list(range(500)), "s": "x" * 5000})
    assert len(out["xs"]) == 200 and len(out["s"]) <= 2000


# ---------------------------------------------------------------- 鉴权 / 限流 / 上限

def test_rejects_when_token_not_configured(tmp_path):
    """不提供「无鉴权也能往库里写告警」的默认 —— 这是安全底线。"""
    client, cfg, s = make_client(tmp_path)
    r = post_hook(client, "grafana", {"alerts": []})
    assert r.status_code == 401, r.text
    assert "未配置" in r.json()["detail"]
    assert client.get("/api/external/alerts").json()["count"] == 0


def test_rejects_wrong_token_and_accepts_header_or_query(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "s3cret")
    body = {"alerts": [{"fingerprint": "f1", "labels": {"alertname": "A"},
                        "annotations": {"summary": "x"}}]}
    assert post_hook(client, "grafana", body, token="wrong").status_code == 401
    assert post_hook(client, "grafana", body).status_code == 401
    assert post_hook(client, "grafana", body, token="s3cret").status_code == 200
    # query 形式（腾讯云/GCP 的推送 URL 由自己给，塞 query 最省事）
    r = client.post("/api/hooks/grafana?token=s3cret", json=body)
    assert r.status_code == 200


def test_per_source_token_overrides_global(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "global-token")
    set_token(client, "grafana-only", source="grafana")
    body = {"alerts": [{"fingerprint": "f1", "labels": {"alertname": "A"}}]}
    assert post_hook(client, "grafana", body, token="global-token").status_code == 401
    assert post_hook(client, "grafana", body, token="grafana-only").status_code == 200


def test_rate_limit_returns_429(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    body = {"alerts": [{"fingerprint": "f", "labels": {"alertname": "A"}}]}
    codes = [post_hook(client, "grafana", body, token="tk").status_code
             for _ in range(hooks.RATE_LIMIT_PER_MIN + 3)]
    assert 200 in codes and 429 in codes, codes
    hooks.rate_reset()


def test_payload_size_cap(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    big = {"alerts": [{"fingerprint": "f", "labels": {"alertname": "A"},
                       "annotations": {"summary": "x" * (hooks.MAX_BODY_BYTES + 10)}}]}
    r = post_hook(client, "grafana", big, token="tk")
    assert r.status_code == 413, r.status_code


def test_unknown_source_is_404(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    assert post_hook(client, "prometheus", {}, token="tk").status_code == 404


# ---------------------------------------------------------------- 落库 / 去重 / 只读

def test_ingest_persists_and_dedupes(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    body = {"alerts": [{"fingerprint": "fp1", "status": "firing",
                        "labels": {"alertname": "HighCPU", "severity": "critical"},
                        "annotations": {"summary": "CPU 高"}}]}
    assert post_hook(client, "grafana", body, token="tk").json()["received"] == 1
    assert post_hook(client, "grafana", body, token="tk").json()["received"] == 1
    items = client.get("/api/external/alerts").json()["items"]
    assert len(items) == 1, "同 source+source_id 必须幂等"
    assert items[0]["source"] == "grafana" and items[0]["severity"] == "critical"
    assert items[0]["labels"]["alertname"] == "HighCPU"


def test_stored_raw_is_redacted(tmp_path):
    """raw_json 落库前必须脱敏：库里不能留下明文 token / webhook URL 参数。"""
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    body = {"alerts": [{"fingerprint": "fp1", "labels": {"alertname": "A"},
                        "annotations": {"summary": "x", "auth_token": "leak-me",
                                        "help": "https://h.example.com/x?token=leak2&k=1"}}]}
    post_hook(client, "grafana", body, token="tk")
    raw = s.db.execute("SELECT raw_json FROM external_alerts").fetchone()["raw_json"]
    assert "leak-me" not in raw and "leak2" not in raw
    assert "token=***" in raw


def test_hooks_module_has_no_outbound_client(tmp_path):
    """只读硬约束：hooks 模块里不得出现任何对外 HTTP 客户端。"""
    src = (Path(hooks.__file__)).read_text(encoding="utf-8")
    for bad in ("urlopen", "requests.", "http.client", "socket.socket"):
        assert bad not in src, "hooks.py 出现了对外客户端：%s" % bad


def test_ingest_does_not_touch_the_outbound_http_client(tmp_path, monkeypatch):
    """再把对外 HTTP 客户端掐死跑一遍：接收路径确实不依赖任何对外连接。

    注意不能掐 socket：TestClient 自己的事件循环就用 socket（Windows Proactor 下会直接
    把测试框架掐死，而不是被测代码）。这里掐的是会真正发起外呼的那两个入口。
    """
    import urllib.request
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止对外网络")))
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("禁止对外网络")))
    body = {"alerts": [{"fingerprint": "f1", "labels": {"alertname": "A"}}]}
    assert post_hook(client, "grafana", body, token="tk").status_code == 200


# ---------------------------------------------------------------- 关联去重（核心）

def test_external_alert_links_to_local_incident(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    nid, token = register(client, cfg, "web01")
    tid = make_task(client, "checkout-api")
    open_incident(client, nid, token, tid)

    body = {"alerts": [{"fingerprint": "fp1", "labels": {"alertname": "API down",
                                                         "instance": "checkout-api"}}]}
    post_hook(client, "grafana", body, token="tk")

    d = client.get("/api/oncall").json()
    it = d["items"][0]
    assert len(it["external"]) == 1, it["external"]      # 本地卡带上旁证
    assert it["external"][0]["source"] == "grafana"
    assert d["external"] == {"firing": 0, "linked": 1}   # 已关联 → 不再单独成卡
    assert not [g for g in d["groups"] if g["kind"] == "external"]


def test_unlinked_firing_external_becomes_its_own_card(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    register(client, cfg, "web01")
    body = {"alerts": [{"fingerprint": "fp9", "labels": {"alertname": "SomethingElse",
                                                        "instance": "not-ours"}}]}
    post_hook(client, "grafana", body, token="tk")
    d = client.get("/api/oncall").json()
    ext = [g for g in d["groups"] if g["kind"] == "external"]
    assert len(ext) == 1 and ext[0]["bucket"] == "live"
    assert d["external"] == {"firing": 1, "linked": 0}
    assert "第三方" in ext[0]["subtitle"]


def test_correlation_matches_chinese_task_names(tmp_path):
    """中文任务名必须能关联上：切词若用 [0-9a-z] 会把中文名整体切碎（线上实测踩到）。"""
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    nid, token = register(client, cfg, "web01")
    tid = make_task(client, "支付网关")
    open_incident(client, nid, token, tid)
    post_hook(client, "grafana", {"alerts": [{"fingerprint": "zh1",
                                              "labels": {"instance": "支付网关"}}]}, token="tk")
    d = client.get("/api/oncall").json()
    it = [i for i in d["items"] if i["task_id"] == tid][0]
    assert len(it["external"]) == 1, it["external"]
    assert d["external"]["linked"] == 1


def test_correlation_ignores_short_name_false_positives(tmp_path):
    """词元匹配而非子串：节点 n1 不能命中 n10。"""
    from gpm.server.api_web import _ext_tokens
    assert "n10" in _ext_tokens({"title": "alert n10", "labels": {}})
    assert "n1" not in _ext_tokens({"title": "alert n10", "labels": {}})


def test_resolved_external_does_not_create_a_card(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    register(client, cfg, "web01")
    body = {"alerts": [{"fingerprint": "fp3", "status": "resolved",
                        "labels": {"alertname": "Old", "instance": "gone"}}]}
    post_hook(client, "grafana", body, token="tk")
    d = client.get("/api/oncall").json()
    assert d["external"]["firing"] == 0
    assert not [g for g in d["groups"] if g["kind"] == "external"]


# ---------------------------------------------------------------- 汇总 / 设置

def test_summary_by_source(tmp_path):
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    post_hook(client, "grafana", {"alerts": [
        {"fingerprint": "g1", "status": "firing", "labels": {"alertname": "A"}},
        {"fingerprint": "g2", "status": "resolved", "labels": {"alertname": "B"}}]},
        token="tk")
    post_hook(client, "zabbix", {"event_id": "z1", "trigger": "T", "status": "PROBLEM"},
              token="tk")
    sm = client.get("/api/external/summary?days=1").json()
    by = {x["source"]: x for x in sm["sources"]}
    assert by["grafana"]["firing"] == 1 and by["grafana"]["resolved"] == 1
    assert by["zabbix"]["firing"] == 1


def test_sla_reports_external_by_source(tmp_path):
    """第六期 29：SLA 报表加「按来源」维度。

    **不编造 MTTA**：外部告警是只读接入，没有本平台的确认动作，所以只给「平均持续时间」
    并在 mtta_note 里说明原因 —— 报表里出现一个算不出来的指标比不出现更糟。
    """
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    now = int(time.time())
    post_hook(client, "grafana", {"alerts": [
        {"fingerprint": "f1", "status": "firing", "labels": {"alertname": "A"}},
        {"fingerprint": "f2", "status": "resolved", "labels": {"alertname": "B"},
         # 相对 now 生成：曾经硬编码 "2026-10-03T10:00:00Z"，SLA 查询窗却是相对
         # now 的滑动窗——硬编码日期滑出窗口后测试每天 UTC 10:00 后恒红（时间炸弹）
         "startsAt": now - 3600, "endsAt": now - 1800}]},
        token="tk")
    post_hook(client, "zabbix", {"event_id": "z1", "trigger": "T", "status": "PROBLEM"},
              token="tk")

    rep = client.get("/api/report/sla?t_from=%d&t_to=%d" % (now - 86400, now + 60)).json()
    ext = rep["external"]
    by = {x["source"]: x for x in ext["sources"]}
    assert by["grafana"]["total"] == 2 and by["grafana"]["firing"] == 1
    assert by["grafana"]["resolved"] == 1
    assert by["grafana"]["avg_duration_s"] == 1800.0, by["grafana"]
    assert by["zabbix"]["firing"] == 1 and by["zabbix"]["avg_duration_s"] is None
    assert "MTTA" in ext["mtta_note"] and "不编造" in ext["mtta_note"]
    # 与本地事件分开列：第三方口径不同，不能混进可用率计算
    assert "incidents" in rep and "overall" in rep


def test_settings_reports_configured_without_leaking_token(tmp_path):
    client, cfg, s = make_client(tmp_path)
    d = client.get("/api/external/settings").json()
    assert all(x["configured"] is False for x in d["sources"])
    assert d["token_header"] == hooks.TOKEN_HEADER and d["limits"]["rate_per_min"] > 0
    set_token(client, "top-secret-token")
    d2 = client.get("/api/external/settings").json()
    assert all(x["configured"] is True for x in d2["sources"])
    assert "top-secret-token" not in json.dumps(d2), "设置接口绝不能回显 Token"


def test_manual_correlate_relinks_after_task_rename(tmp_path):
    """先来的告警可能因为那时还没建任务而没关联上；建好之后要能手工补关联。"""
    client, cfg, s = make_client(tmp_path)
    set_token(client, "tk")
    nid, token = register(client, cfg, "web01")
    post_hook(client, "grafana", {"alerts": [{"fingerprint": "fp7",
                                              "labels": {"instance": "late-task"}}]},
              token="tk")
    assert client.get("/api/oncall").json()["external"]["linked"] == 0
    tid = make_task(client, "late-task")
    open_incident(client, nid, token, tid)
    r = client.post("/api/external/correlate?days=7")
    assert r.status_code == 200 and r.json()["linked"] >= 1
    it = client.get("/api/oncall").json()["items"][0]
    assert len(it["external"]) == 1
