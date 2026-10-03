"""集成测试：节点分组（组级分配）、注册 Token 管理、节点侧事件、资源时序、GeoIP 定位。"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server import geo
from gpm.server.app import create_app


def make_client(tmp_path):
    from gpm.server.storage import Storage
    cfg = Config({"server": {"database": str(tmp_path / "g.db"), "listen": "127.0.0.1:0"}})
    storage = Storage(str(tmp_path / "g.db"))
    return TestClient(create_app(cfg, storage)), cfg, storage


def register(client, cfg, name, token=None):
    reg_token = token or cfg.agent["register_token"]
    r = client.post("/api/agent/register", json={
        "name": name, "register_token": reg_token, "system": {"os": "linux"},
        "local_ip": "10.0.0.9"})
    assert r.status_code == 200, r.text
    nid = r.json()["node_id"]
    return nid, hashlib.sha256(f"{name}:{reg_token}".encode()).hexdigest()


# ---------------- 节点分组 / 组级任务分配 ----------------

def test_group_level_task_assignment(tmp_path):
    client, cfg, s = make_client(tmp_path)
    a, tok_a = register(client, cfg, "g-a")
    b, tok_b = register(client, cfg, "g-b")

    # 建组并把 a 放进去
    g = client.post("/api/groups", json={"name": "华东电信", "note": "测试"}).json()
    client.put(f"/api/groups/{g['id']}/members", json={"nodes": [a]})
    gs = client.get("/api/groups").json()
    assert gs[0]["name"] == "华东电信" and gs[0]["members"] == [a] and gs[0]["member_names"] == ["g-a"]
    # 重名 409
    assert client.post("/api/groups", json={"name": "华东电信"}).status_code == 409
    # 空名 422
    assert client.post("/api/groups", json={"name": "  "}).status_code == 422

    # 任务分配：只给这个组
    tid = client.post("/api/tasks", json={
        "name": "grp-task", "type": "ping", "target": "1.1.1.1",
        "nodes": [f"g:{g['id']}"]}).json()["id"]
    # 组内节点拿得到任务；组外拿不到
    sync_a = client.post("/api/agent/sync", json={
        "node_id": a, "token": tok_a, "config_version": 0}).json()
    assert [t["id"] for t in sync_a["tasks"]] == [tid]
    sync_b = client.post("/api/agent/sync", json={
        "node_id": b, "token": tok_b, "config_version": 0}).json()
    assert sync_b["tasks"] == []

    # 用「组名」写法同样生效
    s.set_group_members(g["id"], [b], int(time.time()))
    sync_b2 = client.post("/api/agent/sync", json={
        "node_id": b, "token": tok_b, "config_version": 0}).json()
    assert [t["id"] for t in sync_b2["tasks"]] == [tid]

    # 删组：成员清空、任务里的 g: 引用被摘掉、config_version 递增
    v0 = s.config_version()
    client.request("DELETE", f"/api/groups/{g['id']}")
    assert client.get("/api/groups").json() == []
    assert s.get_task(tid)["nodes"] == []
    assert s.config_version() > v0
    assert client.request("DELETE", f"/api/groups/{g['id']}").status_code == 404


# ---------------- 注册 Token 管理 ----------------

def test_register_token_lifecycle(tmp_path):
    client, cfg, s = make_client(tmp_path)
    # 建 Token，明文只返回一次
    r = client.post("/api/tokens", json={"name": "机房A", "note": "测试"}).json()
    plain = r["token"]
    assert plain.startswith("gpm_") and "token_hash" not in r
    assert r["enabled"] == 1
    listed = client.get("/api/tokens").json()["items"]
    assert len(listed) == 1 and "token_hash" not in listed[0]

    # 新 Token 可用于注册；引导 Token 仍可用（兼容）
    nid, node_token = register(client, cfg, "tk-node", token=plain)
    assert s.node_by_id(nid)["token_id"] == r["id"]
    assert client.post("/api/agent/register", json={
        "name": "boot-node", "register_token": cfg.agent["register_token"]}).status_code == 200
    # 错误 Token 被拒
    assert client.post("/api/agent/register", json={
        "name": "bad", "register_token": "nope"}).status_code == 403

    # 吊销：已注册节点同步被拒（需用新 Token 重新注册）
    client.put(f"/api/tokens/{r['id']}", json={"enabled": False})
    assert client.post("/api/agent/sync", json={
        "node_id": nid, "token": node_token, "config_version": 0}).status_code == 401
    assert client.post("/api/agent/results", json={
        "node_id": nid, "token": node_token, "results": []}).status_code == 401
    # 恢复后又能同步
    client.put(f"/api/tokens/{r['id']}", json={"enabled": True})
    assert client.post("/api/agent/sync", json={
        "node_id": nid, "token": node_token, "config_version": 0}).status_code == 200
    # 删除
    assert client.request("DELETE", f"/api/tokens/{r['id']}").status_code == 200
    assert client.get("/api/tokens").json()["items"] == []


# ---------------- 节点侧事件（离线/恢复）----------------

def test_node_offline_and_recovery_events(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, tok = register(client, cfg, "evt-node")
    client.post("/api/agent/sync", json={"node_id": nid, "token": tok, "config_version": 0})

    # 心跳超时 → 离线事件（kind=node），重复 sweep 不重复开
    now = int(time.time())
    s.db.execute("UPDATE nodes SET last_heartbeat=? WHERE id=?", (now - 3600, nid))
    s.db.commit()
    assert s.sweep_offline(60, now) == 1
    assert s.sweep_offline(60, now + 60) == 0
    incs = client.get("/api/query/incidents_all?kind=node").json()
    assert len(incs) == 1 and incs[0]["kind"] == "node" and incs[0]["ended_at"] is None
    assert incs[0]["title"].startswith("节点离线") and incs[0]["node_name"] == "evt-node"

    # 恢复：心跳把事件关闭并记时长
    client.post("/api/agent/sync", json={"node_id": nid, "token": tok, "config_version": 0})
    incs = client.get("/api/query/incidents_all?kind=node").json()
    assert incs[0]["ended_at"] is not None and incs[0]["duration_ms"] >= 0
    assert incs[0]["title"].startswith("节点恢复")
    # 时长计算单独验证（同步路径常在同一秒内完成，duration 为 0）
    s.db.execute("UPDATE nodes SET status='offline' WHERE id=?", (nid,))
    s.db.commit()
    iid = s.node_incident_open(nid, now, {"event": "offline"})
    s.node_incident_close(nid, now + 120)
    assert s.db.execute("SELECT duration_ms FROM incidents WHERE id=?",
                        (iid,)).fetchone()["duration_ms"] == 120000

    # 探测类事件与节点类事件互不影响
    tid = client.post("/api/tasks", json={
        "name": "p", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    ts0 = int(time.time()) - 30
    client.post("/api/agent/results", json={"node_id": nid, "token": tok, "results": [
        {"ts": ts0 + i, "task_id": tid, "type": "ping", "status": "fail",
         "error_class": "timeout", "metrics": {}} for i in range(3)]})
    kinds = {i["kind"] for i in client.get("/api/query/incidents_all").json()}
    assert kinds == {"node", "probe"}


# ---------------- 节点资源时序 ----------------

def test_node_metrics_series(tmp_path):
    client, cfg, s = make_client(tmp_path)
    nid, tok = register(client, cfg, "m-node")
    base = int(time.time()) - 1800
    for i in range(5):
        client.post("/api/agent/sync", json={
            "node_id": nid, "token": tok, "config_version": 0,
            "stats": {"version": "0.1.0", "cpu": 10 + i, "mem": 50 + i, "tasks": 1}})
        s.db.execute("UPDATE node_heartbeats SET ts=? WHERE node_id=? AND ts=(SELECT MAX(ts) FROM node_heartbeats WHERE node_id=?)",
                     (base + i * 300, nid, nid))
    s.db.commit()
    r = client.get(f"/api/nodes/metrics?node_id={nid}&t_from={base - 60}&t_to={base + 2000}&bucket=300").json()
    assert r["bucket"] == 300 and len(r["series"]) == 1
    pts = r["series"][0]["points"]
    assert len(pts) == 5, pts
    assert pts[0]["cpu"] == 10 and pts[-1]["cpu"] == 14
    assert pts[0]["mem"] == 50 and all(p["cpu_max"] >= p["cpu"] for p in pts)
    # 全节点视图
    allr = client.get(f"/api/nodes/metrics?t_from={base - 60}&bucket=300").json()
    assert len(allr["series"]) == 1


# ---------------- GeoIP 定位 ----------------

def test_geo_locate_by_tags_and_region_table(tmp_path, monkeypatch):
    client, cfg, s = make_client(tmp_path)
    a, _ = register(client, cfg, "geo-a")
    b, _ = register(client, cfg, "geo-b")
    client.put(f"/api/nodes/{a}", json={"tags": {"lat": "39.9", "lng": "116.4"}})
    client.put(f"/api/nodes/{b}", json={"tags": {"region": "cn-north"}})

    nodes = client.get("/api/geo/nodes").json()["nodes"]
    by = {n["node_name"]: n for n in nodes}
    assert by["geo-a"]["source"] == "标签坐标" and by["geo-a"]["approx"] is False
    assert by["geo-a"]["lat"] == 39.9 and by["geo-a"]["lng"] == 116.4
    assert by["geo-b"]["source"] == "标签(内置区表)" and by["geo-b"]["place"].startswith("中国 · 华北")

    # 在线查询：mock 掉外部接口（含「服务端出口近似」分支）
    monkeypatch.setattr(geo, "_fetch", lambda ip, timeout=6.0: {
        "ok": True, "lat": 35.6, "lng": 139.7, "place": "日本 · 东京", "isp": "test"})
    c, _ = register(client, cfg, "geo-c")
    # 出口为公网 IP → 按出口 IP 定位（非近似）
    s.db.execute("UPDATE nodes SET egress_ip='203.0.113.9' WHERE id=?", (c,))
    s.db.execute("DELETE FROM geo_cache")     # 清缓存，确保走 _fetch
    s.db.commit()
    got = {n["node_name"]: n for n in client.get("/api/geo/nodes").json()["nodes"]}
    assert "203.0.113.9" in got["geo-c"]["source"] and got["geo-c"]["approx"] is False
    # 出口为回环 → 退回「服务端出口近似」并明确标记 approx
    s.db.execute("UPDATE nodes SET egress_ip='127.0.0.1' WHERE id=?", (c,))
    s.db.execute("DELETE FROM geo_cache")
    s.db.commit()
    d = client.get("/api/geo/nodes").json()
    got = {n["node_name"]: n for n in d["nodes"]}
    assert got["geo-c"]["approx"] is True and "服务端出口近似" in got["geo-c"]["source"]
    assert d["unknown"] == []


# ---------------- 历史对比：指标可选（curl/mtr 不再整页空白）----------------

def test_compare_metric_avail_for_non_ping(tmp_path):
    """历史缺陷：对比只比 rtt_avg，curl/mtr 该列为 NULL → 连「当前时段」都空白。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "cmp-node")
    cid = client.post("/api/tasks", json={
        "name": "cmp-curl", "type": "curl", "target": "",
        "urls": ["https://example.com/a"]}).json()["id"]

    ts0 = int(time.time()) - 2 * 3600          # 放在上一个完整小时里
    res = [{"ts": ts0 + i * 60, "task_id": cid, "type": "curl", "dns": "",
            "url": "https://example.com/a", "status": "ok" if i % 4 else "fail",
            "error_class": "" if i % 4 else "http_0",
            "metrics": {"total_time": 100 + i, "http_code": 200}} for i in range(20)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})
    # 聚合是有层级的：1m 从原始表算，5m 从 1m，1h 从 5m（必须按序重算）
    for bkt in ("1m", "5m", "1h"):
        s.agg_recompute(bkt, ts0 - 3600, ts0 + 7200)

    # curl 任务没有 rtt_avg：延迟指标如实为空
    rtt = client.get(f"/api/compare?task_id={cid}&metric=rtt").json()
    assert rtt["unit"] == "ms" and rtt["has_today"] is False
    # 可用率指标必须有数据，单位为 %，取值 0~100
    av = client.get(f"/api/compare?task_id={cid}&metric=avail").json()
    assert av["unit"] == "%" and av["has_today"] is True, av
    pts = [v for v in av["today"] if v is not None]
    assert pts and all(0 <= v <= 100 for v in pts), pts
    assert av["history_hours"] >= 0 and "has_other" in av
    # 丢包率：curl 未聚合 → 如实为空（不报 500）
    lo = client.get(f"/api/compare?task_id={cid}&metric=loss").json()
    assert lo["unit"] == "%" and lo["has_today"] is False

    # ping 任务三种指标都应有数据（rtt/avail/loss）
    pid = client.post("/api/tasks", json={
        "name": "cmp-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]
    pres = [{"ts": ts0 + i * 60, "task_id": pid, "type": "ping", "status": "ok" if i % 3 else "fail",
             "error_class": "" if i % 3 else "timeout",
             "metrics": {"rtt_avg": 12.0 + i, "loss_rate": 0.0 if i % 3 else 1.0,
                         "sent": 4, "received": 4 if i % 3 else 0}} for i in range(20)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": pres})
    for bkt in ("1m", "5m", "1h"):
        s.agg_recompute(bkt, ts0 - 3600, ts0 + 7200)
    for mk, unit in (("rtt", "ms"), ("avail", "%"), ("loss", "%")):
        r = client.get(f"/api/compare?task_id={pid}&metric={mk}").json()
        assert r["unit"] == unit, (mk, r)
        assert r["has_today"] is True, mk


# ---------------- 历史对比：前一时段 + 对比线时间戳对齐 ----------------

def test_compare_tiny_history_hours_truthy(tmp_path):
    """刚上线几分钟的平台 history_hours 曾被 round(…,1) 归零：0 在前端是 falsy，
    「历史不足 24h 自动切前一时段」永不触发、原因文案走错分支（CI 实例实测发现）。"""
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "tiny-node")
    cid = client.post("/api/tasks", json={
        "name": "tiny-curl", "type": "curl", "target": "",
        "urls": ["https://example.com/a"]}).json()["id"]
    now = int(time.time())
    res = [{"ts": now - 90 + i * 30, "task_id": cid, "type": "curl", "dns": "",
            "url": "https://example.com/a", "status": "ok",
            "metrics": {"total_time": 100 + i, "http_code": 200}} for i in range(3)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})
    r = client.get(f"/api/compare?task_id={cid}&metric=avail").json()
    assert 0 < r["history_hours"] < 1, r["history_hours"]   # 必须是「很小的正数」而不是 0


def test_compare_prev_mode_and_offset_alignment(tmp_path):
    """两个回归点：
    ① 历史不足 24h 时「前一时段」必须能给出两条可比曲线（窗口自适应）；
    ② 对比桶是按偏移取出来的，查表时必须把偏移减回去 —— 历史 bug 是对比线恒为空，
       导致三种模式看上去都是同一条当前曲线（用户反馈「图都是一样的」）。
    """
    client, cfg, s = make_client(tmp_path)
    nid, token = register(client, cfg, "prev-node")
    tid = client.post("/api/tasks", json={
        "name": "prev-ping", "type": "ping", "target": "1.1.1.1"}).json()["id"]

    now = int(time.time())
    cur_start = now // 3600 * 3600
    res = []
    for h in range(4):                       # 最近 4 小时：全成功，rtt=10ms
        base = cur_start - h * 3600 + 60
        res += [{"ts": base + i * 60, "task_id": tid, "type": "ping", "status": "ok",
                 "metrics": {"rtt_avg": 10.0, "loss_rate": 0.0}} for i in range(10)]
    for h in range(4, 8):                    # 再往前 4 小时：一半失败，成功时 rtt=20ms
        base = cur_start - h * 3600 + 60
        res += [{"ts": base + i * 60, "task_id": tid, "type": "ping",
                 "status": "ok" if i % 2 else "fail",
                 "error_class": "" if i % 2 else "timeout",
                 "metrics": {"rtt_avg": 20.0 if i % 2 else None,
                             "loss_rate": 0.0 if i % 2 else 1.0}} for i in range(10)]
    client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": res})
    for bkt in ("1m", "5m", "1h"):
        s.agg_recompute(bkt, cur_start - 8 * 3600, cur_start + 3600)

    # 自动窗口（历史 7h → W=3）也必须两边都有数据
    auto = client.get(f"/api/compare?task_id={tid}&mode=prev&metric=avail").json()
    assert auto["window_hours"] >= 1 and auto["mode"] == "prev"
    assert any(v is not None for v in auto["today"]) and any(v is not None for v in auto["other"])

    # 固定 4 小时窗口，便于断言「最近=100% / 前一时段=50%」的取值是否对齐
    r = client.get(f"/api/compare?task_id={tid}&mode=prev&metric=avail&window_hours=4").json()
    assert r["window_hours"] == 4
    today = [v for v in r["today"] if v is not None]
    other = [v for v in r["other"] if v is not None]
    assert today and other, ("对比线必须有数据", today, other)
    assert r["has_other"] is True
    assert min(today) > 90, today            # 最近时段 ~100%
    assert any(v < 60 for v in other), other  # 前一时段 ~50%（对齐错误时拿不到）

    # 延迟指标：前一时段应为 ~20ms（错位时会全是 None）
    rr = client.get(f"/api/compare?task_id={tid}&mode=prev&metric=rtt&window_hours=4").json()
    o = [v for v in rr["other"] if v is not None]
    assert o and all(15 <= v <= 25 for v in o), o

    # 日历型模式在历史不足时如实返回「无对比数据」
    yd = client.get(f"/api/compare?task_id={tid}&mode=yesterday&metric=avail").json()
    assert yd["has_other"] is False and yd["history_hours"] < 24
    assert yd["today_label"] == "最近24小时"


# ---------------- 自定义「IP 段 → 位置」 ----------------

def test_geo_network_mapping_and_longest_prefix(tmp_path):
    """IDC 内网段（10.10.10.0/24 在上海）应优先于在线查询，并支持只写中文地名。"""
    client, cfg, s = make_client(tmp_path)
    nid, _ = register(client, cfg, "idc-node")

    # 校验：非法 CIDR / 无法解析的地名
    assert client.post("/api/geo/networks", json={"cidr": "10.10.10.0/33", "place": "上海"}).status_code == 422
    assert client.post("/api/geo/networks", json={"cidr": "10.10.10.0/24", "place": "火星基地"}).status_code == 422
    # 中文地名直接解析成坐标（内置区表）
    r = client.post("/api/geo/networks", json={
        "cidr": "10.10.10.0/24", "place": "上海", "note": "上海 IDC A 区"}).json()
    assert (r["lat"], r["lng"]) == (31.23, 121.47), r
    # 重复网段 409
    assert client.post("/api/geo/networks", json={"cidr": "10.10.10.0/24", "place": "上海"}).status_code == 409

    # 节点本机 IP 落在段内 → 命中自定义网段（而不是在线查询/服务端出口近似）
    s.db.execute("UPDATE nodes SET local_ip='10.10.10.7' WHERE id=?", (nid,))
    s.db.commit()
    got = {n["node_name"]: n for n in client.get("/api/geo/nodes").json()["nodes"]}
    assert "自定义网段" in got["idc-node"]["source"], got["idc-node"]
    assert "10.10.10.0/24" in got["idc-node"]["source"]
    assert got["idc-node"]["place"] == "上海", got["idc-node"]        # 自由地名保留用户写法
    assert (got["idc-node"]["lat"], got["idc-node"]["lng"]) == (31.23, 121.47)
    # 写区表「键」时用规范地名
    client.post("/api/geo/networks", json={"cidr": "10.20.0.0/16", "place": "cn-east"})
    r3 = [x for x in client.get("/api/geo/networks").json() if x["cidr"] == "10.20.0.0/16"][0]
    assert "华东" in r3["place"], r3

    # 最长前缀优先：再加 10.0.0.0/8 → 北京，10.10.10.7 仍应按 /24 命中上海
    client.post("/api/geo/networks", json={"cidr": "10.0.0.0/8", "place": "北京"})
    got = {n["node_name"]: n for n in client.get("/api/geo/nodes").json()["nodes"]}
    assert "10.10.10.0/24" in got["idc-node"]["source"], got["idc-node"]["source"]

    # 显式经纬度优先于地名解析
    r2 = client.post("/api/geo/networks", json={
        "cidr": "192.168.0.0/16", "place": "自建机房", "lat": 22.5, "lng": 114.0}).json()
    assert (r2["lat"], r2["lng"]) == (22.5, 114.0)
    # 删除
    assert client.request("DELETE", f"/api/geo/networks/{r['id']}").status_code == 200
    assert len(client.get("/api/geo/networks").json()) == 3   # /24 + /8 + 10.20.0.0/16
    assert client.request("DELETE", f"/api/geo/networks/{r['id']}").status_code == 404
    # 区表接口可列出地名（供前端下拉）
    places = client.get("/api/geo/places").json()
    assert any(p["key"] == "shanghai" for p in places) and len(places) > 30
