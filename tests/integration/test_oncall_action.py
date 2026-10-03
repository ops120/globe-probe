"""第三期（行动闭环）回归：深链可达 / 通知链接可配置 / 卡片内可粘贴命令 / 同期变更。

每条都对着一个现场问题（.docs/ONCALL_OPTIMIZATION_2.md 第三期 11-14）：
- 通知里「点击查看」发的是 {public_url}/index.html?task=..&ts=..，而服务端只路由了 "/"，
  线上实测该链接 **404**；验收脚本又特意在 /index.html 404 时退回 "/" 继续断言，两边都没发现。
- public_url 只有 setting_get 一条来源、**没有任何地方写过它**：没有 config 键也没有接口，
  等于线上根本配不了 → 每条通知都没有【链接】段落。
- 卡片上的「建议」是散文，值班的人还得自己想命令；「刚改完就炸」这条最省时间的线索
  只在事件详情弹窗里，第一屏看不到。
- 节点卡片原先的「去处理」调 oncallGoto('') 直接 return，点了毫无反应（第一期已改「看节点」）。
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path, server_extra=None):
    from gpm.server.storage import Storage
    db = str(tmp_path / "act.db")
    srv = {"database": db, "listen": "127.0.0.1:0"}
    srv.update(server_extra or {})
    cfg = Config({"server": srv})
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


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
    r = client.post("/api/agent/results", json={"node_id": nid, "token": token,
                                               "results": res})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- 11. 深链可达

def test_index_html_route_serves_the_app(tmp_path):
    """/index.html 必须可达：通知深链用的就是这个路径（线上实测曾是 404）。"""
    client, cfg, s = make_client(tmp_path)
    for path in ("/", "/index.html"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert "text/html" in r.headers.get("content-type", ""), path
        assert "<html" in r.text.lower(), path


def test_deeplink_query_survives_on_index_html(tmp_path):
    """深链形式必须能带着 query 打开（前端 boot 只解析 location.search）。"""
    client, cfg, s = make_client(tmp_path)
    r = client.get("/index.html?task=t1&ts=123")
    assert r.status_code == 200 and "<html" in r.text.lower()


# ---------------------------------------------------------------- 12. 通知链接可配置

def test_public_url_defaults_to_unconfigured(tmp_path):
    client, cfg, s = make_client(tmp_path)
    d = client.get("/api/settings/public-url").json()
    assert d["public_url"] == "" and d["configured"] is False
    assert "config.yaml" in d["hint"]          # 要告诉运维「去哪配」，不只是说没配
    assert client.get("/api/oncall").json()["public_url_configured"] is False


def test_public_url_set_and_clear(tmp_path):
    client, cfg, s = make_client(tmp_path)
    r = client.put("/api/settings/public-url", json={"public_url": "https://gpm.example.com/"})
    assert r.status_code == 200, r.text
    assert r.json() == {"public_url": "https://gpm.example.com", "configured": True}
    assert client.get("/api/settings/public-url").json()["public_url"] == "https://gpm.example.com"
    assert client.get("/api/oncall").json()["public_url_configured"] is True

    r2 = client.put("/api/settings/public-url", json={"public_url": ""})
    assert r2.json()["configured"] is False
    assert client.get("/api/oncall").json()["public_url_configured"] is False


def test_public_url_rejects_non_http(tmp_path):
    """不编造链接：非 http(s) 直接 422，而不是存下一个点不开的值。"""
    client, cfg, s = make_client(tmp_path)
    for bad in ("not-a-url", "ftp://x/", "//host/path"):
        assert client.put("/api/settings/public-url",
                          json={"public_url": bad}).status_code == 422, bad


def test_public_url_from_config_is_applied_at_startup(tmp_path):
    """config.yaml 的 server.public_url 要生效 —— 原先这个键根本不存在。"""
    from gpm.server import alerting
    client, cfg, s = make_client(tmp_path, {"public_url": "https://from-config.example.com/"})
    assert alerting.public_url(s) == "https://from-config.example.com"
    assert client.get("/api/settings/public-url").json()["configured"] is True


def test_notification_link_uses_the_configured_base(tmp_path):
    """通知里的链接前缀必须来自配置，且路径为 /index.html（该路径现已可达）。"""
    from gpm.server import alerting
    client, cfg, s = make_client(tmp_path, {"public_url": "https://gpm.example.com/"})
    line = alerting._link_line(s, "t1", 123)
    assert line == "【链接】https://gpm.example.com/index.html?task=t1&ts=123", line
    # 未配置 → 整段省略（不编造链接）
    s.setting_set("public_url", "")
    assert alerting._link_line(s, "t1", 123) == ""


# ---------------------------------------------------------------- 13. 可粘贴命令

def test_runbook_available_per_layer():
    from gpm.server.diagnose import RUNBOOK, runbook_for
    assert RUNBOOK, "RUNBOOK 不能为空"
    for layer, cmd in RUNBOOK.items():
        assert cmd.strip(), layer
    assert "mtr" in runbook_for("网络层")
    assert "openssl" in runbook_for("TLS 层")
    assert "nc -vz" in runbook_for("网络/端口层")
    assert runbook_for("不存在的层面") == ""      # 不编造命令
    assert runbook_for("") == ""


def test_oncall_items_and_groups_carry_runbook(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "t")
    open_incident(client, nid, token, tid)
    d = client.get("/api/oncall").json()
    it = d["items"][0]
    assert it["layer"] == "网络层" and it["runbook"] == runbook_expected("网络层")
    g = d["groups"][0]
    assert g["runbook"] == it["runbook"]


def runbook_expected(layer):
    from gpm.server.diagnose import runbook_for
    return runbook_for(layer)


# ---------------------------------------------------------------- 14. 同期变更

def test_card_shows_related_audit_changes(tmp_path):
    """事件窗口 ±30 分钟内、动过该任务/节点的写操作要出现在卡片上。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "changed-task")
    now = int(time.time())
    # 事件开始前 10 分钟：有人停用过这个任务
    s.audit_add(now - 600, "tester", "停用任务", "changed-task", target_id=tid,
                detail="changed-task: enabled 1 -> 0")
    open_incident(client, nid, token, tid, ts0=now - 60)

    it = client.get("/api/oncall").json()["items"][0]
    assert len(it["changes"]) == 1, it["changes"]
    assert it["changes"][0]["action"] == "停用任务"
    assert "changed-task" in it["changes"][0]["detail"]
    assert client.get("/api/oncall").json()["groups"][0]["changes"], "聚合卡也要带变更"


def test_card_ignores_unrelated_and_old_changes(tmp_path):
    """只保留与该任务/节点直接相关的：泛泛列一堆无关审计等于噪声。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "mine")
    other = make_task(client, "someone-else")
    now = int(time.time())
    s.audit_add(now - 300, "tester", "修改任务", "someone-else", target_id=other,
                detail="someone-else: interval 10 -> 30")   # 别的任务 → 不显示
    s.audit_add(now - 7200, "tester", "停用任务", "mine", target_id=tid,
                detail="mine: enabled 1 -> 0")              # 窗口外（>30min）→ 不显示
    open_incident(client, nid, token, tid, ts0=now - 60)

    it = client.get("/api/oncall").json()["items"][0]
    assert it["changes"] == [], it["changes"]


def test_changes_capped_per_card(tmp_path):
    """卡片是提示不是日志：只留最近几条（完整清单在事件详情弹窗里）。"""
    from gpm.server.api_web import CHANGES_PER_CARD
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "n1")
    tid = make_task(client, "busy")
    now = int(time.time())
    for i in range(CHANGES_PER_CARD + 3):
        s.audit_add(now - 300 + i, "tester", "修改任务", "busy", target_id=tid,
                    detail="busy: change %d" % i)
    open_incident(client, nid, token, tid, ts0=now - 60)
    ch = client.get("/api/oncall").json()["items"][0]["changes"]
    assert len(ch) == CHANGES_PER_CARD
    assert ch[0]["ts"] > ch[-1]["ts"], "应按时间倒序（最近的在前）"


def test_changes_ignored_for_node_events(tmp_path):
    """节点侧事件没有目标任务，不联动同期变更（与事件详情弹窗口径一致）。"""
    client, cfg, s = make_client(tmp_path)
    nid, _ = register(client, cfg, "lonely")
    now = int(time.time())
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (now - 300, nid))
    s.db.commit()
    s.sweep_offline(60, now)
    d = client.get("/api/oncall").json()
    assert d["items"][0]["task_id"] == ""
    assert d["items"][0]["changes"] == []
