"""导入导出（第九期）回归。

每条新端点行为都固化成用例（断言口径以 api_web 实现为准）：
- CSV 统一前置 UTF-8 BOM + Content-Disposition 文件名（Excel 打开中文不乱码）
- 记录类导出（incidents/alerts/external_alerts）：json 分支字段已反序列化、
  窗口/条件过滤生效；CSV 行数 = 造的记录数 + 表头
- /api/export/config：五类对象齐全、任务 urls/params/dns 等是对象不是 json 字符串、
  tokens / nodes（运行时注册产物）顶层键故意不含
- /api/import/config：upsert 保留 id 真改字段、create 保留任务 id 让规则引用仍成立、
  单项失败不拖垮整体、坏结构 400、admin_token 门禁（读侧仍开放）
- cfg.export.max_rows 是硬上限：limit 只能收紧不能放大
- 导入后 /api/tasks 缓存立即失效，新任务不用等 TTL 就可见
"""
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app
from gpm.server.storage import Storage

# 时间全部用固定常量。导出端点的默认窗是「最近 7 天/24h」（相对 now()），
# 固定的历史时刻必须显式传窗参数，否则全部落在窗外。
T0 = 1_700_000_000
WIN_FROM = T0 - 3600
WIN_TO = T0 + 3600
OLD = T0 - 8 * 86400           # 默认 7 天窗外


def make_client(tmp_path, extra=None):
    """仿 test_jev.make_client；extra 按节合并进 Config（export / admin_token 用）。"""
    db = str(tmp_path / "ie.db")
    raw = {"server": {"database": db, "listen": "127.0.0.1:0"}}
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    cfg = Config(raw)
    storage = Storage(db)
    return TestClient(create_app(cfg, storage)), cfg, storage


def make_task(client, name, **kw):
    body = {"name": name, "type": "ping", "target": "1.1.1.1", "interval_seconds": 10}
    body.update(kw)
    r = client.post("/api/tasks", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def add_alert(s, ts, rid, rname, delivered=True, key="k1"):
    """alert_add 需要的最小 rule 形状（id/name/metric）。"""
    return s.alert_add(ts, {"id": rid, "name": rname, "metric": "avail"}, key,
                       "firing", "标题", "正文", {"node": "n1"}, delivered,
                       2, 2 if delivered else 0)


def csv_rows(text):
    """按 CSV 解析响应体（含 BOM 的首格不参与比较，只数行）。"""
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------- CSV BOM 与文件名

def test_csv_exports_start_with_bom_and_filename(tmp_path):
    """四类 CSV 导出都以 BOM 开头且带 attachment 文件名；行数 = 表头 + 数据。"""
    client, _cfg, s = make_client(tmp_path)
    tid = make_task(client, "bom")
    nid, _ = s.register_node("bom-node", "h", {}, "0.1.0", {}, T0)
    s.insert_results([{"ts": T0, "task_id": tid, "node_id": nid, "type": "ping",
                       "status": "ok", "metrics": {"rtt_ms": 1.5}}], T0)
    s.incident_open(tid, nid, "", "", T0, {"error_class": "timeout"})
    add_alert(s, T0, "r1", "规则一")
    s.external_alert_upsert("prometheus", "p-1",
                            {"title": "宕机", "status": "firing", "started_at": T0,
                             "labels": {"job": "node"}}, T0)

    win = f"t_from={WIN_FROM}&t_to={WIN_TO}"
    cases = [
        (f"/api/export?task_id={tid}&{win}&fmt=csv", f"export_{tid}"),
        (f"/api/export/incidents?{win}&fmt=csv", "gpm-incidents"),
        (f"/api/export/alerts?{win}&fmt=csv", "gpm-alerts"),
        ("/api/export/external_alerts?fmt=csv", "gpm-external-alerts"),
    ]
    for url, base in cases:
        r = client.get(url)
        assert r.status_code == 200, r.text
        assert r.text.startswith("\ufeff"), f"{url} 缺 UTF-8 BOM"
        cd = r.headers["content-disposition"]
        assert "attachment" in cd and f'filename="{base}.csv"' in cd, cd
        assert len(csv_rows(r.text)) == 2, f"{url} 应为表头 + 1 行数据"


# ---------------------------------------------------------------- incidents 导出

def test_export_incidents_json_fields_window_and_csv_rows(tmp_path):
    """事件导出：json 分支 open/reason 已解析；窗外事件不出现；CSV 行数对得上。"""
    client, _cfg, s = make_client(tmp_path)
    tid = make_task(client, "inc")
    iid_open = s.incident_open(tid, "n1", "", "", T0 - 100, {"error_class": "timeout"})
    iid_closed = s.incident_open(tid, "n1", "", "", T0 - 200, {"error_class": "dns"})
    s.incident_close(iid_closed, T0 - 50)
    s.incident_open(tid, "n1", "", "", OLD, {"error_class": "timeout"})  # 窗外

    r = client.get(f"/api/export/incidents?fmt=json&t_from={WIN_FROM}&t_to={WIN_TO}")
    assert r.status_code == 200, r.text
    data = json.loads(r.text)
    assert [d["id"] for d in data] == [iid_open, iid_closed], "按开始时间倒序且窗外不出现"
    d_open = data[0]
    assert d_open["open"] == 1 and not d_open["ended_at"], "未恢复事件 open=1 且 ended_at 为空"
    assert d_open["reason"] == {"error_class": "timeout"}, "reason 必须解析成对象"
    d_closed = data[1]
    assert d_closed["open"] == 0 and d_closed["ended_at"], "已收口事件 open=0"

    r2 = client.get(f"/api/export/incidents?fmt=csv&t_from={WIN_FROM}&t_to={WIN_TO}")
    assert r2.status_code == 200, r2.text
    assert len(csv_rows(r2.text)) == 3                    # 表头 + 2 条窗内事件


# ---------------------------------------------------------------- alerts 导出

def test_export_alerts_fields_and_rule_filter(tmp_path):
    """告警导出：json 分支带 rule_id/rule_name/delivered；rule_id 过滤生效。"""
    client, _cfg, s = make_client(tmp_path)
    make_task(client, "alerts")
    add_alert(s, T0, "r1", "规则一", delivered=True, key="k1")
    add_alert(s, T0 - 120, "r2", "规则二", delivered=False, key="k2")

    r = client.get(f"/api/export/alerts?fmt=json&t_from={WIN_FROM}&t_to={WIN_TO}")
    assert r.status_code == 200, r.text
    data = json.loads(r.text)
    assert [d["rule_id"] for d in data] == ["r1", "r2"]   # ts 倒序
    a = data[0]
    assert a["rule_name"] == "规则一" and a["metric"] == "avail"
    assert a["delivered"] and a["status"] == "firing"

    r2 = client.get(
        f"/api/export/alerts?fmt=json&rule_id=r2&t_from={WIN_FROM}&t_to={WIN_TO}")
    data2 = json.loads(r2.text)
    assert [d["rule_id"] for d in data2] == ["r2"] and not data2[0]["delivered"]


# ---------------------------------------------------------------- external_alerts 导出

def test_export_external_alerts_deserialized_and_source_filter(tmp_path):
    """第三方告警导出：labels/raw 已反序列化成对象；source 过滤生效。"""
    client, _cfg, s = make_client(tmp_path)
    s.external_alert_upsert("alertmanager", "am-1",
                            {"title": "宕机", "severity": "critical", "status": "firing",
                             "started_at": T0, "labels": {"job": "node", "dc": "sh"}}, T0)
    s.external_alert_upsert("zabbix", "zb-1",
                            {"title": "端口丢包", "status": "resolved",
                             "started_at": T0 - 60, "labels": {"host": "sw-1"}}, T0)

    r = client.get("/api/export/external_alerts?fmt=json")
    assert r.status_code == 200, r.text
    data = json.loads(r.text)
    assert {d["source"] for d in data} == {"alertmanager", "zabbix"}
    am = next(d for d in data if d["source"] == "alertmanager")
    assert am["source_id"] == "am-1" and am["title"] == "宕机"
    assert am["labels"] == {"job": "node", "dc": "sh"}, "labels 必须是对象而非 json 字符串"

    r2 = client.get("/api/export/external_alerts?fmt=json&source=zabbix")
    data2 = json.loads(r2.text)
    assert len(data2) == 1 and data2[0]["source_id"] == "zb-1"


# ---------------------------------------------------------------- 配置导出

def test_export_config_shape_without_tokens_and_nodes(tmp_path):
    """备份五类对象齐全、对象字段已反序列化；tokens/nodes 顶层键故意不含。"""
    client, _cfg, s = make_client(tmp_path)
    tid = make_task(client, "cfg-curl", type="curl", target="",
                    urls=["https://example.com/api"], params={"method": "GET"})
    nid, _ = s.register_node("cfg-node", "h", {}, "0.1.0", {}, T0)
    s.create_channel("ch1", "飞书群", "webhook",
                     {"url": "https://open.example/hook/xyz", "secret": "SecABC"}, T0)
    s.create_rule("r1", {"name": "可用率", "metric": "avail", "op": "<", "threshold": 99,
                         "task_id": tid, "channel_ids": ["ch1"], "severity": "critical"}, T0)
    s.create_group("g1", "华东", "", T0)
    s.set_group_members("g1", [nid], T0)
    s.create_window("w1", {"name": "停发窗口", "starts_at": T0, "ends_at": T0 + 3600,
                           "task_id": tid, "node_id": "", "note": ""}, T0)

    r = client.get("/api/export/config")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["kind"] == "gpm-config-backup"
    assert "tokens" not in p and "nodes" not in p, "agent 凭据与运行时注册产物不进备份"

    t = next(x for x in p["tasks"] if x["id"] == tid)
    assert t["urls"] == ["https://example.com/api"] and t["params"] == {"method": "GET"}
    assert isinstance(t["params"], dict) and isinstance(t["dns"], list)
    assert t["nodes"] == [], "任务级节点分配保留（与顶层运行时 nodes 键是两回事）"
    ch = next(x for x in p["channels"] if x["id"] == "ch1")
    assert ch["config"]["secret"] == "SecABC", "渠道 config（含密钥）原样进备份"
    ru = next(x for x in p["rules"] if x["id"] == "r1")
    assert ru["channel_ids"] == ["ch1"] and ru["task_id"] == tid
    g = next(x for x in p["groups"] if x["id"] == "g1")
    assert g["members"] == [nid]
    w = next(x for x in p["windows"] if x["id"] == "w1")
    assert (w["starts_at"], w["ends_at"], w["task_id"]) == (T0, T0 + 3600, tid)


# ---------------------------------------------------------------- 配置导入

def test_import_config_upsert_updates_existing(tmp_path):
    """upsert：同 id 再导入走更新——计数为 updated，字段真被改，渠道停用生效。"""
    client, _cfg, s = make_client(tmp_path)
    tid = make_task(client, "老名字")
    s.create_channel("ch1", "渠道", "webhook", {"url": "https://a/b"}, T0)

    body = {"tasks": [{"id": tid, "name": "导入改名", "type": "ping", "target": "9.9.9.9",
                       "urls": [], "params": {}, "dns": ["223.5.5.5"],
                       "interval_seconds": 30}],
            "channels": [{"id": "ch1", "name": "渠道", "type": "webhook",
                          "config": {"url": "https://a/b"}, "enabled": False}]}
    r = client.post("/api/import/config", json=body)
    assert r.status_code == 200, r.text
    imp = r.json()["imported"]
    assert imp["tasks"]["updated"] == 1 and imp["tasks"]["created"] == 0
    assert imp["channels"]["updated"] == 1
    assert not imp["tasks"]["errors"] and not imp["channels"]["errors"]

    t = s.get_task(tid)
    assert t["name"] == "导入改名" and t["target"] == "9.9.9.9"
    assert t["dns"] == ["223.5.5.5"] and t["interval_seconds"] == 30
    assert next(c for c in s.list_channels() if c["id"] == "ch1")["enabled"] == 0


def test_import_config_create_preserves_ids_and_refs(tmp_path):
    """删任务后导入备份：created 计数、任务 id 原样保留、规则引用仍指向真实对象。"""
    client, _cfg, s = make_client(tmp_path)
    tid = make_task(client, "被删的任务", type="curl", target="",
                    urls=["https://example.com/api"], params={"method": "GET"})
    s.create_channel("ch1", "渠道", "webhook", {"url": "https://a/b"}, T0)
    s.create_rule("r1", {"name": "可用率", "metric": "avail", "op": "<", "threshold": 99,
                         "task_id": tid, "channel_ids": ["ch1"]}, T0)

    backup = client.get("/api/export/config").json()
    s.delete_task(tid, T0)
    assert s.get_task(tid) is None

    r = client.post("/api/import/config", json=backup)
    assert r.status_code == 200, r.text
    imp = r.json()["imported"]
    assert imp["tasks"]["created"] == 1 and imp["tasks"]["updated"] == 0
    assert not imp["tasks"]["errors"]
    assert imp["channels"]["updated"] == 1 and imp["rules"]["updated"] == 1

    t = s.get_task(tid)
    assert t is not None and t["id"] == tid, "任务 id 必须原样保留（规则引用靠它）"
    assert t["urls"] == ["https://example.com/api"] and t["params"] == {"method": "GET"}
    ru = next(x for x in s.list_rules() if x["id"] == "r1")
    assert ru["task_id"] == tid and s.get_task(ru["task_id"]) is not None
    assert ru["channel_ids"] == ["ch1"]
    assert {"ch1"} <= {c["id"] for c in s.list_channels()}


def test_import_config_partial_failure_does_not_block_rest(tmp_path):
    """单项失败只进 errors，不拖垮整体：好任务/好规则照常入库并计数。"""
    client, _cfg, s = make_client(tmp_path)
    body = {"tasks": [{"id": "t-ok-1", "name": "好任务", "type": "ping",
                       "target": "1.1.1.1"}],
            "rules": [{"id": "r-ok", "name": "好规则", "metric": "avail", "op": "<",
                       "threshold": 99, "task_id": "t-ok-1"},
                      {"name": "坏规则（缺 id，实现里必报错）"}]}
    r = client.post("/api/import/config", json=body)
    assert r.status_code == 200, r.text
    imp = r.json()["imported"]
    assert imp["tasks"]["created"] == 1 and s.get_task("t-ok-1") is not None
    assert imp["rules"]["created"] == 1
    assert imp["rules"]["errors"], "缺 id 的规则必须进 errors"
    assert any("缺 id" in e for e in imp["rules"]["errors"])
    assert next(x for x in s.list_rules() if x["id"] == "r-ok")["task_id"] == "t-ok-1"


def test_import_config_rejects_non_backup_payload(tmp_path):
    """不像备份的结构（缺全部五类数组）必须 400，而不是静默空导入。"""
    client, _cfg, _s = make_client(tmp_path)
    for bad in ({}, {"foo": 1}):
        r = client.post("/api/import/config", json=bad)
        assert r.status_code == 400, bad
        assert "配置备份" in r.json()["detail"]


def test_import_config_admin_token_gate(tmp_path):
    """配置 admin_token 后导入（写）要 token；导出（读）与查询同一信任模型保持开放。"""
    client, _cfg, s = make_client(tmp_path, {"server": {"admin_token": "sekrit"}})
    body = {"tasks": [{"id": "t-gate", "name": "门禁任务", "type": "ping",
                       "target": "1.1.1.1"}]}

    r = client.post("/api/import/config", json=body)
    assert r.status_code == 403, r.text
    assert s.get_task("t-gate") is None, "被拒的导入不得产生副作用"
    r2 = client.post("/api/import/config", json=body, headers={"X-Admin-Token": "wrong"})
    assert r2.status_code == 403

    r3 = client.post("/api/import/config", json=body, headers={"X-Admin-Token": "sekrit"})
    assert r3.status_code == 200, r3.text
    assert r3.json()["imported"]["tasks"]["created"] == 1

    r4 = client.get("/api/export/config")
    assert r4.status_code == 200, "读侧导出不要求 token"


# ---------------------------------------------------------------- max_rows 硬上限

def test_export_alerts_max_rows_hard_cap(tmp_path):
    """cfg.export.max_rows 是硬上限：limit 只能收紧（1→1），不能放大（999999→cap）。"""
    client, _cfg, s = make_client(tmp_path, {"export": {"max_rows": 2}})
    make_task(client, "cap")
    for i in range(5):
        add_alert(s, T0 - i * 10, "r1", "规则一", key=f"k{i}")

    url = f"/api/export/alerts?fmt=json&t_from={WIN_FROM}&t_to={WIN_TO}"
    assert len(json.loads(client.get(url).text)) == 2, "默认必须截到 max_rows"
    assert len(json.loads(client.get(url + "&limit=1").text)) == 1, "limit 可再收紧"
    assert len(json.loads(client.get(url + "&limit=999999").text)) == 2, "limit 不能放大"


# ---------------------------------------------------------------- 导入后任务列表立即生效

def test_import_config_invalidates_tasks_list_cache(tmp_path):
    """/api/tasks 有 TTL 缓存：导入新建任务后必须立即可见，不等 TTL。"""
    client, _cfg, _s = make_client(tmp_path)
    assert [t["id"] for t in client.get("/api/tasks").json()] == []   # 预热缓存

    body = {"tasks": [{"id": "t-fresh", "name": "新导入", "type": "ping",
                       "target": "1.1.1.1"}]}
    assert client.post("/api/import/config", json=body).status_code == 200

    ids = [t["id"] for t in client.get("/api/tasks").json()]          # 不带 fresh=1
    assert "t-fresh" in ids
