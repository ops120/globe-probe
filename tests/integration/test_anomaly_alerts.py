"""动态基线端到端（第十期）：真实 Storage + TestClient 全链路。

覆盖：params 可行域校验挡入口（422）→ /api/baseline 预览 → anomaly 规则评估
firing/resolved（走真实告警状态机）→ 关联分析第三故障源（kind=alert 进簇/零散）→
JEV 基线偏离证据。基线历史用真实 SQL 种进 aggregates（1h 桶，14 天）。
"""
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from gpm.config import Config  # noqa: E402
from gpm.server import baseline as _bl  # noqa: E402
from gpm.server import hooks  # noqa: E402
from gpm.server.app import create_app  # noqa: E402

DAY = 86400


def make_client(tmp_path):
    from gpm.server.storage import Storage
    db = str(tmp_path / "anomaly.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0"}})
    storage = Storage(db)
    hooks.rate_reset()
    return TestClient(create_app(cfg, storage)), cfg, storage


def make_task(client, name="anomaly-e2e"):
    r = client.post("/api/tasks", json={
        "name": name, "type": "ping", "target": "223.5.5.5", "interval_seconds": 30})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def seed_history(storage, task_id, base=10.0, days=14, cur_values=None):
    """种 1h 聚合桶：每天同小时±1 共 3 桶（对称摆动 9/11），当前评估桶可指定。

    锚定必须与 baseline 的对齐口径一致：本地午夜 + d 天 + 本地小时——
    用 UTC 午夜会与本地小时对齐错位 8 小时，基线直接变空（实测踩坑）。"""
    import datetime as _dt
    now = int(time.time())
    cur_ts = now // 3600 * 3600 - 3600
    cur_hour = time.localtime(cur_ts).tm_hour
    aligned = {(cur_hour + off) % 24 for off in (-1, 0, 1)}
    day0 = _dt.datetime.fromtimestamp(cur_ts).replace(hour=0, minute=0, second=0,
                                                      microsecond=0).timestamp()
    with storage.lock:
        for d in range(0, days + 1):
            for h in sorted(aligned):
                ts = int(day0) + h * 3600 - d * DAY
                if ts > cur_ts:
                    continue                            # 未来桶不种
                v = base + (1 if d % 2 else -1)
                storage.db.execute(
                    "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                    "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                    " VALUES('1h',?,?, 'n1','','',10,9,1,?,0.1,0.9)", (ts, task_id, v))
        for i, v in (cur_values or {}).items():
            storage.db.execute(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                " VALUES('1h',?,?, 'n1','','',10,9,1,?,0.1,0.9)", (cur_ts - i * 3600, task_id, v))
        storage.db.commit()


def test_anomaly_firing_and_resolved(tmp_path):
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    now = int(time.time())
    # 14 天历史 10±1ms，最近两小时 50ms（台阶式劣化，远超 3σ）
    seed_history(s, tid, cur_values={0: 50.0, 1: 50.0})
    r = client.post("/api/alerts/rules", json={
        "name": "延迟异常基线", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "rtt_avg", "k": 3, "min_samples": 20,
                    "min_consecutive": 2, "baseline_days": 14}})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    ev = client.post("/api/alerts/evaluate").json()["events"]
    fired = [e for e in ev if e.get("kind") == "firing"]
    assert fired, ev
    assert "动态基线" in fired[0]["title"]
    last = s.alert_last(rid, tid)
    assert last and last["status"] == "firing"
    assert "中位" in last["text"] and "MAD" in last["text"], "告警文本必须带基线数字"

    # 静默期：立即再评估不重复打扰
    ev2 = client.post("/api/alerts/evaluate").json()["events"]
    assert not [e for e in ev2 if e.get("kind") == "firing"]

    # 恢复：当前桶回到基线带宽内 → resolved
    seed_history(s, tid, cur_values={0: 10.5})
    ev3 = client.post("/api/alerts/evaluate").json()["events"]
    resolved = [e for e in ev3 if e.get("kind") == "resolved"]
    assert resolved, ev3
    last = s.alert_last(rid, tid)
    assert last and last["status"] == "resolved"


def test_anomaly_validation_gates(tmp_path):
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    # weekday_hour × 14 天 × min_samples=20 死锁 → 422（可行域联动校验挡在入口）
    r = client.post("/api/alerts/rules", json={
        "name": "坏基线", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "rtt_avg", "align": "weekday_hour",
                    "min_samples": 20, "baseline_days": 14}})
    assert r.status_code == 422 and "不可行" in r.json()["detail"], r.text
    # anomaly 必须指定任务
    r = client.post("/api/alerts/rules", json={"name": "坏基线2", "metric": "anomaly"})
    assert r.status_code == 422 and "任务" in r.json()["detail"]
    # params 只属于 anomaly
    r = client.post("/api/tasks", json={"name": "普通", "type": "ping",
                                        "target": "223.5.5.5", "interval_seconds": 30})
    tid2 = r.json()["id"]
    r = client.post("/api/alerts/rules", json={
        "name": "阈值规则带params", "metric": "avail", "op": "lt", "threshold": 0.95,
        "task_id": tid2, "params": {"k": 3}})
    assert r.status_code == 422 and "params" in r.json()["detail"]


def test_baseline_preview_endpoint(tmp_path):
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    seed_history(s, tid)
    r = client.get(f"/api/baseline?task_id={tid}&metric_field=rtt_avg")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["evaluable"] and body["streams"][0]["samples"] >= 20
    assert "center" in body["streams"][0]
    # 不可行域参数 → 422；未知任务 → 404
    r = client.get(f"/api/baseline?task_id={tid}&align=weekday_hour&min_samples=20")
    assert r.status_code == 422
    assert client.get("/api/baseline?task_id=nope").status_code == 404


def test_correlation_includes_anomaly_alert(tmp_path):
    """第三故障源：anomaly firing 告警进关联分析（不开事件也可见）。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    seed_history(s, tid, cur_values={0: 50.0, 1: 50.0})
    rid = client.post("/api/alerts/rules", json={
        "name": "基线规则", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "rtt_avg"}}).json()["id"]
    client.post("/api/alerts/evaluate")
    rep = client.get("/api/correlation?hours=24").json()
    kinds = [m["kind"] for c in rep["clusters"] for m in c["members"]] + \
            [m["kind"] for m in rep["singles"]]
    assert "alert" in kinds, kinds


def test_jev_baseline_evidence(tmp_path):
    """JEV 基线偏离证据：同任务近 1h 的 anomaly firing 告警进证据池。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    seed_history(s, tid, cur_values={0: 50.0, 1: 50.0})
    rid = client.post("/api/alerts/rules", json={
        "name": "基线规则", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "rtt_avg"}}).json()["id"]
    client.post("/api/alerts/evaluate")
    rows = s.recent_anomaly_alerts(tid, int(time.time()) - 3600)
    assert rows, "recent_anomaly_alerts 应取到 firing 的基线告警"
    from gpm.server import jev
    detail = {"incident": {"task_id": tid, "started_at": int(time.time()) - 300,
                            "reason": {"error_class": "timeout"}}}
    ev = jev.build_evidence(detail, storage=s)
    kinds = [e["kind"] for e in ev]
    assert "baseline" in kinds, kinds


# ---------------- 复核第二轮补钉（2026-10-06） ----------------

def test_mixed_bounded_and_zscore_streams_no_ringing(tmp_path):
    """混合 bounded + zscore 流：恢复必须逐流 all() 判定（复核 P1-2 漏网处）。

    曾用 max(eff_k) 跨流聚合：bounded 流（eff_k=1）仍越界却被判恢复，
    下一轮又 firing → 每 30s 振铃刷通知。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    # 造两条流：n1 恒定可用率史（bounded），n2 波动延迟史（zscore）
    now = int(time.time())
    cur_ts = now // 3600 * 3600 - 3600
    cur_hour = time.localtime(cur_ts).tm_hour
    import datetime as dt
    day0 = int(dt.datetime.fromtimestamp(cur_ts).replace(hour=0, minute=0, second=0,
                                                     microsecond=0).timestamp())
    aligned = {(cur_hour + off) % 24 for off in (-1, 0, 1)}
    with s.lock:
        for d in range(0, 15):
            for h in sorted(aligned):
                ts = day0 + h * 3600 - d * 86400
                if ts > cur_ts:
                    continue
                s.db.execute(
                    "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                    "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                    " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,1.0)", (ts, tid))
                s.db.execute(
                    "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                    "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                    " VALUES('1h',?,?,'n2','','',10,9,1,?,0.05,0.95)", (ts, tid,
                                                                             10.0 + (d % 2)))
        # 当前两桶：n1 可用率跌到 0.94（bounded 越界 z≈1.2）；n2 延迟 50（zscore 越界）
        for i in (0, 1):
            s.db.execute(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                " VALUES('1h',?,'x','n1','','',10,6,4,10.0,0.4,0.6)".replace("'x'", "?"),
                (cur_ts - i * 3600, tid))
            # n2 的可用率也越界（跌到 0.90 = 带宽 0.05 的 1.0 倍）→ 两流都 firing
            s.db.execute(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                " VALUES('1h',?,?,'n2','','',10,9,1,50.0,0.1,0.90)", (cur_ts - i * 3600, tid))
        s.db.commit()
    rid = client.post("/api/alerts/rules", json={
        "name": "混合流", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "avail_rate", "k": 3, "min_samples": 20,
                   "min_consecutive": 2, "baseline_days": 14}}).json()["id"]
    ev = client.post("/api/alerts/evaluate").json()["events"]
    assert [e for e in ev if e.get("kind") == "firing"], ev
    # 只让 n1 恢复（回带宽内），n2 仍越界 → 不得 resolved
    for i in (0, 1):
        s.db.execute(
            "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
            "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
            " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,1.0)", (cur_ts - i * 3600, tid))
    s.db.commit()
    ev2 = client.post("/api/alerts/evaluate").json()["events"]
    assert not [e for e in ev2 if e.get("kind") == "resolved"], \
        "n2 仍越界时不得恢复（all() 语义）"
    last = s.alert_last(rid, tid)
    assert last and last["status"] == "firing", "应维持 firing"


def test_anomaly_escalate_forced_zero(tmp_path):
    """anomaly 规则的 escalate_minutes 强制 0（不开事件→无升级依据，复核 P1-3）。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    r = client.post("/api/alerts/rules", json={
        "name": "升级禁用", "metric": "anomaly", "task_id": tid,
        "escalate_minutes": 30,
        "params": {"metric_field": "avail_rate"}})
    assert r.status_code == 200, r.text
    assert r.json()["escalate_minutes"] == 0, "anomaly 的 escalate 必须被强制清零"
    # node 范围同样被清空（评估按全任务流走，静默丢弃语义不一致）
    r2 = client.post("/api/alerts/rules", json={
        "name": "node范围清空", "metric": "anomaly", "task_id": tid,
        "node_id": "n1", "params": {"metric_field": "avail_rate"}})
    assert r2.status_code == 200 and r2.json()["node_id"] == ""


def test_alert_last_same_second_tiebreak(tmp_path):
    """同秒 firing→resolved 双行：alert_last 必须读回最新（id DESC 竞态修复钉）。

    这正是 anomaly 高频评估实测踩到的存量 bug：不带二级排序时 SQLite 顺序不定。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    seed_history(s, tid, cur_values={0: 50.0, 1: 50.0})
    rid = client.post("/api/alerts/rules", json={
        "name": "同秒", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "rtt_avg"}}).json()["id"]
    client.post("/api/alerts/evaluate")            # firing
    seed_history(s, tid, cur_values={0: 10.5})     # 同秒内恢复
    client.post("/api/alerts/evaluate")            # resolved（同 ts）
    last = s.alert_last(rid, tid)
    assert last and last["status"] == "resolved", \
        f"同秒双行时 alert_last 必须读回最新的 resolved（实际 {last and last['status']}）"
