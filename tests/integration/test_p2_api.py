"""P2 集成测试：tcp/dns 任务类型（校验/钳制/接入）、params 校验共用 helper、mtr_trend 聚合端点。"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from gpm.common.models import TaskCreate, validate_params  # noqa: E402
from gpm.config import Config  # noqa: E402
from gpm.server.app import create_app  # noqa: E402


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "test.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    app = create_app(cfg, storage)
    return TestClient(app), cfg, storage


# ---------------------------------------------------------------- models：params 校验


def test_task_create_tcp_and_dns_targets():
    t = TaskCreate(name="tcp-ok", type="tcp", target="127.0.0.1:8625")
    assert t.target == "127.0.0.1:8625"
    assert TaskCreate(name="tcp-host", type="tcp", target="example.com",
                      params={"port": 443}).target == "example.com"
    assert TaskCreate(name="tcp-v6", type="tcp", target="[2001:db8::1]:443").target
    d = TaskCreate(name="dns-ok", type="dns", target="example.com", dns=["223.5.5.5"])
    assert d.type == "dns"
    # 非法形态
    with pytest.raises(ValidationError):
        TaskCreate(name="bad-port", type="tcp", target="1.1.1.1:70000")
    with pytest.raises(ValidationError):
        TaskCreate(name="no-target", type="tcp", target="")
    with pytest.raises(ValidationError):
        TaskCreate(name="bad-domain", type="dns", target="bad domain!")
    with pytest.raises(ValidationError):
        TaskCreate(name="bad-domain2", type="dns", target="")


def test_validate_params_shapes():
    # curl 合法
    validate_params("curl", {"method": "head", "ip_version": "6",
                             "headers": {"X-A": "1", "X-B": "2"}, "body": "hi",
                             "follow_redirects": False, "cert_check": True})
    with pytest.raises(ValueError):
        validate_params("curl", {"method": "BREW"})
    with pytest.raises(ValueError):
        validate_params("curl", {"regex": "(unclosed"})
    with pytest.raises(ValueError):
        validate_params("curl", {"headers": {"a": {"nested": 1}}})
    with pytest.raises(ValueError):
        validate_params("curl", {"body": "x" * 9000})
    with pytest.raises(ValueError):
        validate_params("curl", {"ip_version": "8"})
    with pytest.raises(ValueError):
        validate_params("curl", "not-a-dict")
    # tcp
    validate_params("tcp", {"port": 8625, "tls": True, "cert_min_days": 15})
    with pytest.raises(ValueError):
        validate_params("tcp", {"port": 0})
    with pytest.raises(ValueError):
        validate_params("tcp", {"port": "443"})
    with pytest.raises(ValueError):
        validate_params("tcp", {"tls": "yes"})
    # dns
    validate_params("dns", {"expected_ips": ["1.2.3.4", "10.0.0.0/8"],
                            "expected_regex": r"\d+"})
    with pytest.raises(ValueError):
        validate_params("dns", {"expected_ips": ["not-an-ip"]})
    with pytest.raises(ValueError):
        validate_params("dns", {"expected_ips": "1.2.3.4"})
    with pytest.raises(ValueError):
        validate_params("dns", {"expected_regex": "["})
    # mtr
    validate_params("mtr", {"probe_mode": "tcp", "show_asn": True})
    with pytest.raises(ValueError):
        validate_params("mtr", {"probe_mode": "gre"})
    # ping
    with pytest.raises(ValueError):
        validate_params("ping", {"ip_version": "6.5"})


# ---------------------------------------------------------------- API：类型校验/钳制/接入


def test_p2_task_types_via_api(tmp_path):
    client, _cfg, s = make_client(tmp_path)
    # tcp 任务创建成功，间隔下限 10s
    r = client.post("/api/tasks", json={"name": "tcp-loop", "type": "tcp",
                                        "target": "127.0.0.1:8625", "interval_seconds": 10})
    assert r.status_code == 200, r.text
    tcp_id = r.json()["id"]
    assert r.json()["interval_seconds"] == 10
    # tcp 非法目标
    assert client.post("/api/tasks", json={"name": "tcp-bad", "type": "tcp",
                                           "target": "1.1.1.1:70000"}).status_code == 422
    # tcp 低于下限 → 422（模型 ge=10）
    assert client.post("/api/tasks", json={"name": "tcp-fast", "type": "tcp",
                                           "target": "127.0.0.1:1",
                                           "interval_seconds": 3}).status_code == 422
    # dns 任务：间隔 10 → 服务端钳到 30
    r = client.post("/api/tasks", json={"name": "dns-mon", "type": "dns",
                                        "target": "example.com", "interval_seconds": 10,
                                        "dns": ["223.5.5.5"],
                                        "params": {"expected_regex": r"\d+"}})
    assert r.status_code == 200, r.text
    dns_id = r.json()["id"]
    assert r.json()["interval_seconds"] == 30
    # dns 非法目标 / 非法 expected
    assert client.post("/api/tasks", json={"name": "dns-bad", "type": "dns",
                                           "target": "a b c"}).status_code == 422
    assert client.post("/api/tasks", json={"name": "dns-bad2", "type": "dns",
                                           "target": "example.com",
                                           "params": {"expected_ips": ["oops"]}}).status_code == 422
    # 更新：非法 regex params → 422（共用 validate_params）
    curl_id = client.post("/api/tasks", json={
        "name": "curl-p2", "type": "curl", "target": "127.0.0.1",
        "urls": ["http://127.0.0.1:8620/api/health"],
        "params": {"keyword": "ok"}}).json()["id"]
    assert client.put(f"/api/tasks/{curl_id}",
                      json={"params": {"regex": "("}}).status_code == 422
    assert client.put(f"/api/tasks/{curl_id}",
                      json={"params": {"method": "TRACE2"}}).status_code == 422
    # 更新：合法 params + tcp 任务 host:port 目标放行；dns 间隔下限在更新同样生效
    assert client.put(f"/api/tasks/{tcp_id}",
                      json={"target": "127.0.0.1:8625"}).status_code == 200
    assert client.put(f"/api/tasks/{dns_id}",
                      json={"interval_seconds": 10}).status_code == 200
    tasks = {t["id"]: t for t in client.get("/api/tasks").json()}
    assert tasks[dns_id]["interval_seconds"] == 30

    # 接入：tcp/dns 类型的结果可入库（协议 type 扩展）
    reg = client.post("/api/agent/register", json={"name": "p2-node",
                                                   "register_token": _cfg.agent["register_token"]})
    nid = reg.json()["node_id"]
    import hashlib
    token = hashlib.sha256(f"p2-node:{_cfg.agent['register_token']}".encode()).hexdigest()
    ts = int(time.time())
    b = client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": [
        {"ts": ts, "task_id": tcp_id, "type": "tcp", "dns": "", "url": "", "status": "ok",
         "metrics": {"rtt_ms": 1.2, "rtt_avg": 1.2, "port": 8625}},
        {"ts": ts + 1, "task_id": dns_id, "type": "dns", "dns": "", "url": "", "status": "ok",
         "metrics": {"lines": {"udp:223.5.5.5": {"ok": True, "answers": ["93.184.216.34"],
                                                 "ttl": 300, "ms": 12.0}},
                     "consistent": True, "rtt_ms": 12.0, "rtt_avg": 12.0, "changed": False}},
    ]}).json()
    assert b["accepted"] == 2, b
    # rtt_avg 聚合可用（tcp 的 rtt_ms 同写 rtt_avg）
    from gpm.server.storage import BUCKET_SECONDS
    step = BUCKET_SECONDS["1m"]
    b_from = ts // step * step
    s.agg_recompute("1m", b_from, ts + 60)
    rows = s.agg_read("1m", tcp_id, nid, "", "", b_from, ts + 60)
    assert rows and rows[0]["rtt_avg"] == 1.2


def test_mtr_trend_endpoint(tmp_path):
    client, _cfg, s = make_client(tmp_path)
    reg = client.post("/api/agent/register", json={"name": "mtr-node",
                                                   "register_token": _cfg.agent["register_token"]})
    nid = reg.json()["node_id"]
    tid = client.post("/api/tasks", json={"name": "mtr-t", "type": "mtr", "target": "8.8.8.8",
                                          "interval_seconds": 60}).json()["id"]
    ts = int(time.time())
    row = lambda t, hops, st="ok": {  # noqa: E731
        "ts": t, "task_id": tid, "node_id": nid, "type": "mtr", "dns": "", "url": "",
        "status": st, "error_class": "", "error": "", "dns_server": "",
        "resolved_ip": "8.8.8.8", "dns_time_ms": None,
        "metrics": {"cycles": 10, "mode": "mtr", "hops": hops}, "config_version": 1}
    ins = [
        row(ts - 20, [{"hop": 1, "host": "10.0.0.1", "loss_pct": 0.0, "avg": 2.0, "asn": None},
                      {"hop": 2, "host": "8.8.8.8", "loss_pct": 0.0, "avg": 50.0, "asn": 15169}]),
        row(ts - 10, [{"hop": 1, "host": "10.0.0.1", "loss_pct": 20.0, "avg": 3.0, "asn": None},
                      {"hop": 2, "host": "8.8.8.8", "loss_pct": 100.0, "avg": 0.0, "asn": 15169}]),
    ]
    inserted, _dups = s.insert_results(ins, ts)
    assert inserted == 2
    r = client.get(f"/api/query/mtr_trend?task_id={tid}&hours=24").json()
    assert r["window"]["hours"] == 24 and r["window"]["results"] == 2
    hops = r["hops"]
    assert [h["hop"] for h in hops] == [1, 2]
    h1, h2 = hops
    assert h1["seen"] == 2 and h1["loss_avg"] == 10.0 and h1["rtt_avg"] == 2.5
    assert h1["host"] == "10.0.0.1" and h1["asn"] is None
    assert h2["seen"] == 2 and h2["loss_avg"] == 50.0
    assert h2["rtt_avg"] == 50.0, "全超时跳的 avg=0 不应计入均值"
    assert h2["asn"] == 15169
    # hours 越界钳制 + node_id 过滤
    r2 = client.get(f"/api/query/mtr_trend?task_id={tid}&hours=99999&node_id={nid}").json()
    assert r2["window"]["hours"] == 168 and len(r2["hops"]) == 2
    r3 = client.get(f"/api/query/mtr_trend?task_id={tid}&node_id=no-such-node").json()
    assert r3["hops"] == [] and r3["window"]["results"] == 0
    # 空任务 → 空跳数组
    r4 = client.get("/api/query/mtr_trend?task_id=t-none&hours=1").json()
    assert r4["hops"] == []


def test_series_lines_for_dns_detail(tmp_path):
    """dns 详情「逐线路表」数据源：GET /api/query/series?metric=lines&granularity=raw
    曾 400「raw 仅支持 rtt/total/loss」→ 前端渲染中断（浏览器验收实测发现）。"""
    client, _cfg, s = make_client(tmp_path)
    dns_id = client.post("/api/tasks", json={
        "name": "dns-lines", "type": "dns", "target": "example.com",
        "interval_seconds": 30, "dns": ["223.5.5.5", "119.29.29.29"]}).json()["id"]
    reg = client.post("/api/agent/register", json={"name": "dns-node",
                                                   "register_token": _cfg.agent["register_token"]})
    nid = reg.json()["node_id"]
    import hashlib
    token = hashlib.sha256(f"dns-node:{_cfg.agent['register_token']}".encode()).hexdigest()
    ts = int(time.time())
    metrics = {"lines": {"udp:223.5.5.5": {"ok": True, "answers": ["93.184.216.34"], "ttl": 30, "ms": 12.5},
                         "udp:119.29.29.29": {"ok": True, "answers": ["93.184.216.34"], "ttl": 31, "ms": 18.0}},
               "consistent": True, "rtt_ms": 15.25, "rtt_avg": 15.25}
    r = client.post("/api/agent/results", json={"node_id": nid, "token": token, "results": [
        {"ts": ts, "task_id": dns_id, "type": "dns", "dns": "", "url": "", "status": "ok",
         "metrics": metrics}]})
    assert r.status_code == 200, r.text
    q = client.get(f"/api/query/series?task_id={dns_id}&node_id={nid}&metric=lines&granularity=raw")
    assert q.status_code == 200, q.text
    pts = q.json()["points"]
    assert pts and pts[-1]["metrics"]["lines"]["udp:223.5.5.5"]["ok"] is True
    assert pts[-1]["metrics"]["consistent"] is True
    # 旧口径不受影响：rtt 仍走 v 列
    q2 = client.get(f"/api/query/series?task_id={dns_id}&node_id={nid}&metric=rtt&granularity=raw")
    assert q2.status_code == 200 and q2.json()["points"][-1]["v"] == 15.25
