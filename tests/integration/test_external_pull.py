"""第三方告警「拉取」适配器（第六期 26）回归。

webhook 推不到时改用 API 拉。这里刻意做了两件事：
1. **一个真实的 HTTP 桩服务**跑通默认抓取路径 —— 只用注入的假 fetch 测不出
   「URL/方法/头/超时」这些真正会出错的地方；
2. **未实现就是未实现**：腾讯云/GCP 必须如实返回「未实现」，不能假装成功。
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server import pullers  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "pull.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


class Stub(BaseHTTPRequestHandler):
    """返回可配置 JSON 的桩服务；记录收到的请求供断言。"""
    payload = b"[]"
    status = 200
    seen: list = []

    def _serve(self):
        Stub.seen.append((self.command, self.path, dict(self.headers)))
        body = Stub.payload
        self.send_response(Stub.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _serve

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self._serve()

    def log_message(self, *a):     # 静音
        pass


def serve_stub():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


# ---------------------------------------------------------------- 能力 / 未实现

def test_supported_sources():
    assert pullers.supported("grafana") is True
    assert pullers.supported("zabbix") is True
    assert pullers.supported("tencent") is False, "腾讯云需要 TC3-HMAC 签名，本期未实现"
    assert pullers.supported("gcp") is False, "GCP 需要 OAuth，本期未实现"
    assert pullers.supported("nope") is False


def test_unimplemented_sources_report_honestly(tmp_path):
    """未实现必须如实返回，而不是发一个假的成功。"""
    client, cfg, s = make_client(tmp_path)
    for src in ("tencent", "gcp"):
        r = client.post("/api/external/pull/%s/run" % src)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["supported"] is False and body["fetched"] == 0
        assert "未实现" in body["error"]
    st = client.get("/api/external/pull").json()["sources"]
    assert {x["source"]: x["supported"] for x in st} == {
        "grafana": True, "zabbix": True, "tencent": False, "gcp": False}


def test_missing_url_is_reported_not_silently_ok(tmp_path):
    client, cfg, s = make_client(tmp_path)
    r = client.post("/api/external/pull/grafana/run").json()
    assert r["supported"] is True and r["fetched"] == 0
    assert "未配置拉取地址" in r["error"]


# ---------------------------------------------------------------- 真实 HTTP（Grafana）

def test_grafana_pull_over_real_http(tmp_path):
    """用真实 HTTP 桩跑默认抓取路径：URL、方法、Authorization 头都要对。"""
    srv, base = serve_stub()
    try:
        client, cfg, s = make_client(tmp_path)
        Stub.payload = json.dumps([
            {"fingerprint": "p1", "status": "firing",
             "labels": {"alertname": "HighCPU", "instance": "web01", "severity": "critical"},
             "annotations": {"summary": "CPU 高"},
             "startsAt": "2026-10-03T10:00:00Z"},
            {"fingerprint": "p2", "status": "resolved",
             "labels": {"alertname": "PingLoss", "instance": "web02"},
             "annotations": {"summary": "恢复"}, "startsAt": "2026-10-03T09:00:00Z"},
        ]).encode()
        client.put("/api/external/pull/grafana",
                   json={"url": base, "token": "tk-1", "enabled": True})
        r = client.post("/api/external/pull/grafana/run").json()
        assert r["fetched"] == 2 and r["created"] == 2, r
        # 请求形状
        method, path, headers = Stub.seen[-1]
        assert method == "GET" and path == "/api/v2/alerts", (method, path)
        assert headers.get("Authorization") == "Bearer tk-1"
        # 落库与状态
        items = client.get("/api/external/alerts").json()["items"]
        assert {x["source_id"] for x in items} == {"p1", "p2"}
        assert client.get("/api/external/pull").json()["sources"][0]["last_ok"] > 0
    finally:
        srv.shutdown()


def test_pull_is_idempotent(tmp_path):
    """同一批告警拉两次：第二次只更新不新增（幂等键 source+source_id）。"""
    srv, base = serve_stub()
    try:
        client, cfg, s = make_client(tmp_path)
        Stub.payload = json.dumps([{"fingerprint": "p1", "labels": {"alertname": "A"}}]).encode()
        client.put("/api/external/pull/grafana", json={"url": base, "enabled": True})
        first = client.post("/api/external/pull/grafana/run").json()
        second = client.post("/api/external/pull/grafana/run").json()
        assert first["created"] == 1 and second["created"] == 0 and second["updated"] == 1
        assert client.get("/api/external/alerts").json()["count"] == 1
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- Zabbix JSON-RPC

def test_zabbix_pull_request_and_parse(tmp_path):
    client, cfg, s = make_client(tmp_path)
    seen = {}

    def fake_fetch(url, method, headers, body):
        seen.update({"url": url, "method": method, "body": json.loads(body.decode())})
        return {"jsonrpc": "2.0", "result": [
            {"triggerid": "11", "description": "Disk full", "priority": "4",
             "lastchange": 1791000000, "value": "1", "hosts": [{"host": "db01"}]},
            {"triggerid": "12", "description": "Recovered", "priority": "2",
             "lastchange": 1791000500, "value": "0", "hosts": [{"host": "db02"}]},
        ], "id": 1}

    client.put("/api/external/pull/zabbix", json={"url": "http://zbx.example.com", "enabled": True})
    r = pullers.poll_source(s, "zabbix", fetch=fake_fetch)
    assert r["fetched"] == 2 and r["created"] == 2
    assert seen["method"] == "POST" and seen["url"].endswith("/api_jsonrpc.php")
    assert seen["body"]["method"] == "trigger.get"
    items = {x["source_id"]: x for x in client.get("/api/external/alerts").json()["items"]}
    assert items["11"]["status"] == "firing" and items["11"]["labels"]["host"] == "db01"
    assert items["12"]["status"] == "resolved", "Zabbix value=0 表示恢复"


def test_zabbix_jsonrpc_error_is_empty_not_crash(tmp_path):
    client, cfg, s = make_client(tmp_path)
    client.put("/api/external/pull/zabbix", json={"url": "http://zbx", "enabled": True})
    r = pullers.poll_source(s, "zabbix", fetch=lambda *a: {"error": {"message": "auth failed"}})
    assert r["fetched"] == 0 and r["error"] == ""


# ---------------------------------------------------------------- 退避 / 调度

def test_failure_sets_backoff_and_excludes_from_due(tmp_path):
    client, cfg, s = make_client(tmp_path)
    client.put("/api/external/pull/grafana", json={"url": "http://127.0.0.1:1", "enabled": True})
    now_s = int(time.time())

    def boom(*a):
        raise OSError("connection refused")

    r = pullers.poll_source(s, "grafana", ts=now_s, fetch=boom)
    assert r["fetched"] == 0 and "connection refused" in r["error"]
    st = pullers.state(s, "grafana")
    assert st["fail_count"] == 1 and st["next_at"] > now_s, st
    assert "grafana" not in pullers.due_sources(s, now_s), "退避期内不该再拉"
    assert "grafana" in pullers.due_sources(s, st["next_at"] + 1)

    # 退避指数增长
    pullers.poll_source(s, "grafana", ts=now_s + 1, fetch=boom)
    assert pullers.state(s, "grafana")["next_at"] - (now_s + 1) == pullers.BACKOFF_BASE * 2


def test_success_clears_backoff(tmp_path):
    srv, base = serve_stub()
    try:
        client, cfg, s = make_client(tmp_path)
        Stub.payload = b"[]"
        client.put("/api/external/pull/grafana", json={"url": base, "enabled": True})
        pullers.poll_source(s, "grafana", fetch=lambda *a: (_ for _ in ()).throw(OSError("x")))
        assert pullers.state(s, "grafana")["fail_count"] == 1
        pullers.poll_source(s, "grafana")
        st = pullers.state(s, "grafana")
        assert st["fail_count"] == 0 and st["next_at"] == 0 and st["last_error"] == ""
    finally:
        srv.shutdown()


def test_due_sources_requires_enabled_supported_configured(tmp_path):
    client, cfg, s = make_client(tmp_path)
    now_s = int(time.time())
    assert pullers.due_sources(s, now_s) == [], "默认全关，不该自己去打外部接口"
    client.put("/api/external/pull/grafana", json={"url": "http://127.0.0.1:1"})
    assert pullers.due_sources(s, now_s) == [], "只配地址、没启用 → 不拉"
    client.put("/api/external/pull/grafana", json={"enabled": True})
    assert pullers.due_sources(s, now_s) == ["grafana"]
    client.put("/api/external/pull/tencent", json={"url": "http://x", "enabled": True})
    assert "tencent" not in pullers.due_sources(s, now_s), "未实现的来源不进调度"


def test_interval_is_clamped(tmp_path):
    client, cfg, s = make_client(tmp_path)
    assert client.put("/api/external/pull/grafana", json={"interval_seconds": 5}).json()[
        "interval_seconds"] == 60
    assert client.put("/api/external/pull/grafana", json={"interval_seconds": 10 ** 9}).json()[
        "interval_seconds"] == 86400


def test_pull_config_never_echoes_token(tmp_path):
    client, cfg, s = make_client(tmp_path)
    client.put("/api/external/pull/grafana", json={"url": "http://x", "token": "secret-tk"})
    st = client.get("/api/external/pull").json()
    assert "secret-tk" not in json.dumps(st)
    assert st["sources"][0]["configured"] is True
