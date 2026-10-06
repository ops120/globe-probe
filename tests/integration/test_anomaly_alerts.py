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


# ---------------- 第三轮复核回归钉（2026-10-06） ----------------

def test_evaluate_endpoint_reports_skipped_without_lease(tmp_path):
    """evaluate 端点与后台循环共用 alert 租约：多实例手动触发不再重复告警/重复外发。

    动态验证实测修复前：两实例各评估一遍 → 6 条外发（应为 3 条）。这里用「租约被他
    人持有」模拟第二实例，断言如实返回 skipped 而非照常评估。"""
    client, cfg, s = make_client(tmp_path)
    from gpm.server.lease import DbLease
    holder = DbLease(s, "alert", "other-instance", ttl=300)
    assert holder.hold() is True          # 别的实例先拿到 alert 租约且未过期
    r = client.post("/api/alerts/evaluate")
    assert r.status_code == 200, r.text
    body = r.json()
    # 无条件下断言：本钉曾经写成「if body.get(skipped)」的条件式，把租约检查整个删掉
    # （= 修复前行为）它照样通过，等于没钉。变异测试：lease 分支改成 if False 时本用例必须红。
    assert body.get("skipped") is True, \
        "他人持有 alert 租约时必须如实 skipped 而非照常评估，实际返回：" + json.dumps(body)
    assert body["events"] == [], body
    assert "租约" in body["reason"], body


def test_evaluate_endpoint_runs_when_lease_is_free(tmp_path):
    """反向对照：租约没人占时 evaluate 端点必须真评估（防钉修过头把功能改坏）。"""
    client, cfg, s = make_client(tmp_path)
    r = client.post("/api/alerts/evaluate")
    assert r.status_code == 200, r.text
    assert not r.json().get("skipped"), r.json()   # 键缺省即「没跳过」
    assert "events" in r.json()


def test_anomaly_text_uses_stream_eff_k_not_global_k(tmp_path):
    """告警文本阈值必须用**流级 eff_k**（bounded=1.0），不是全局 k=3.0（复核 P0）。

    修复前恒定可用率突跌会打印「偏离 1.2σ ≥ 阈值 3.0σ」——自相矛盾，值班人
    读到的数字与实际判定口径不符。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    # avail 口径必须种 avail_rate 列（seed_history 只动 rtt_avg）
    import datetime as _dt
    _now = int(time.time())
    _cur_ts = _now // 3600 * 3600 - 3600
    _hh = time.localtime(_cur_ts).tm_hour
    _al = {(_hh + o) % 24 for o in (-1, 0, 1)}
    _d0 = int(_dt.datetime.fromtimestamp(_cur_ts).replace(hour=0, minute=0, second=0,
                                                         microsecond=0).timestamp())
    with s.lock:
        for d in range(15):
            for h in sorted(_al):
                ts = _d0 + h * 3600 - d * 86400
                if ts > _cur_ts:
                    continue
                av = 1.0          # 恒定历史 → MAD=0 → bounded 绝对带宽分支
                s.db.execute(
                    "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                    "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                    " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,?)", (ts, tid, av))
        for i in (0, 1):
            s.db.execute(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                " VALUES('1h',?,?,'n1','','',10,6,4,10.0,0.4,0.94)", (_cur_ts - i * 3600, tid))
        s.db.commit()
    client.post("/api/alerts/rules", json={
        "name": "口径", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "avail_rate", "k": 3, "min_samples": 20,
                   "min_consecutive": 2, "baseline_days": 14}})
    client.post("/api/alerts/evaluate")
    last = [a for a in s.alert_recent(limit=20)
            if a["rule_name"] == "口径" and a["status"] == "firing"]
    assert last, "应产生 firing"
    txt = last[0]["text"]
    assert "门槛 1.0σ" in txt, f"阈值须用流级 eff_k=1.0，实际文本：\n{txt}"
    assert "绝对带宽" in txt and "MAD" not in txt, "bounded 模式不得把带宽报成 MAD"
    assert "中位" in txt and "样本" in txt


def test_metric_field_illegal_value_rejected(tmp_path):
    """非法 metric_field 必须 422——静默改成 avail_rate 落库=编造（复核 P2）。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    r = client.post("/api/alerts/rules", json={
        "name": "非法字段", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "bogus"}})
    assert r.status_code == 422 and "metric_field" in r.json()["detail"], r.text


def test_remind_text_does_not_claim_consecutive_buckets(tmp_path):
    """remind 的两条触发路径判定依据不同，文案必须各说各的（复核：P0 同类残留）。

    remind 有两条路径：
      a) 本轮仍有流满足「连续 mc 桶」→ 可以说「连续 N 个桶」；
      b) fired 为空、只是**当前桶**仍越界 → 说「连续 N 个桶」就是编造。
    修复前两条共用同一句，实测 b 路径文案写「连续 3 个小时桶」，而紧邻的桶序列里
    最老桶是 0.0σ（只有 2 个桶偏离）——值班人读到自相矛盾的两行。
    顺带钉住 silence_seconds=0 在新建规则时真的落 0（本用例要靠它才能走到 remind）。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    import datetime as _dt
    _now = int(time.time())
    _cur_ts = _now // 3600 * 3600 - 3600
    _hh = time.localtime(_cur_ts).tm_hour
    _al = {(_hh + o) % 24 for o in (-2, -1, 0, 1)}
    _d0 = int(_dt.datetime.fromtimestamp(_cur_ts).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    with s.lock:
        for d in range(15):
            for h in sorted(_al):
                ts = _d0 + h * 3600 - d * 86400
                if ts <= _cur_ts:
                    s.db.execute(
                        "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                        "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                        " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,1.0)", (ts, tid))
        s.db.commit()

    def _put(ts, av):
        with s.lock:
            s.db.execute(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,?)", (ts, tid, av))
            s.db.commit()

    rr = client.post("/api/alerts/rules", json={
        "name": "提醒口径", "metric": "anomaly", "task_id": tid,
        "silence_seconds": 0,
        "params": {"metric_field": "avail_rate", "k": 3, "min_samples": 20,
                   "min_consecutive": 3, "baseline_days": 14}})
    assert rr.status_code == 200, rr.text
    assert rr.json()["silence_seconds"] == 0, "新建规则必须原样落 0（显式 0 不被当缺省）"
    rid = rr.json()["id"]

    for i in (0, 1, 2):                    # 3 个评估桶全偏离 → firing
        _put(_cur_ts - i * 3600, 0.94)
    client.post("/api/alerts/evaluate")
    assert s.alert_last(rid, tid)["title"].startswith("【告警】")

    _put(_cur_ts - 2 * 3600, 1.0)          # 最老桶回基线 → fired 失效、当前桶仍越界
    client.post("/api/alerts/evaluate")
    # 同秒可能写两行，用有 id DESC 兜底的 alert_last 取最新；
    # 注意 remind 行落库 status 仍是 firing（remind = 未恢复告警的再次提醒，
    # 见 alerting._evaluate_impl 的 dkind 归并），要靠标题/正文区分，不能看 status
    last = s.alert_last(rid, tid)
    assert last["title"].startswith("【提醒】"), \
        f"应为提醒通知，实际标题 {last['title']!r}"
    txt = last["text"]
    assert "当前桶仍越界" in txt, f"判定行须如实说明只判了当前桶：\n{txt}"
    assert "未满足连续 3 个小时桶" in txt, txt
    assert "连续 3 个小时桶 ≥" not in txt and "≥ 门槛" in txt, txt
    assert "0.0σ" in txt, "桶序列里最老桶应显示 0.0σ（佐证确实没连续 3 桶）"


def test_anomaly_metric_unit_is_sigma_not_k_sigma(tmp_path):
    """z 值本身就是「偏离几倍 σ」，再乘 k 是二次计量——单位只能标 σ。

    该元信息经 /api/alerts/rules 的 metrics 下发给前端，改它等于改界面口径。"""
    client, cfg, s = make_client(tmp_path)
    m = client.get("/api/alerts/rules").json()["metrics"]
    assert m["anomaly"][2] == "σ", f"anomaly 单位应为 σ，实际 {m['anomaly'][2]!r}"


def test_5m_prewarning_log_labels_sigma_and_prints_threshold(tmp_path, caplog):
    """5m 快路径预警日志同样把 σ 值标成 kσ，且不报门槛（复核 P2 漏改处）。

    实测口径：z5 与告警正文同源（流级 eff_k），日志必须写「Nσ（门槛 Ms）」，
    否则值班人拿日志和告警正文对不上（一个说 1.2kσ 一个说门槛 1.0σ）。"""
    import logging as _lg
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    import datetime as _dt
    _now = int(time.time())
    _cur_ts = _now // 3600 * 3600 - 3600
    _hh = time.localtime(_cur_ts).tm_hour
    _al = {(_hh + o) % 24 for o in (-1, 0, 1)}
    _d0 = int(_dt.datetime.fromtimestamp(_cur_ts).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    with s.lock:
        for d in range(15):
            for h in sorted(_al):
                ts = _d0 + h * 3600 - d * 86400
                if ts <= _cur_ts:
                    s.db.execute(
                        "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                        "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                        " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,1.0)", (ts, tid))
        s.db.commit()
    # 当前 1h 评估桶全部正常 → fired=False，才会走 5m 快路径
    for i in (0, 1):
        with s.lock:
            s.db.execute(
                "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
                "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
                " VALUES('1h',?,?,'n1','','',10,10,0,10.0,0.0,1.0)", (_cur_ts - i * 3600, tid))
            s.db.commit()
    # 5m 桶放一个越界值：z5=(1.0-0.90)/0.05=2.0 ≥ 门槛 1.0
    t5 = _now // 300 * 300 - 300
    with s.lock:
        s.db.execute(
            "INSERT OR REPLACE INTO aggregates(bucket,ts,task_id,node_id,dns,url,"
            "count,ok,fail,rtt_avg,loss_rate,avail_rate)"
            " VALUES('5m',?,?,'n1','','',10,9,1,10.0,0.1,0.90)", (t5, tid))
        s.db.commit()
    client.post("/api/alerts/rules", json={
        "name": "5m预警", "metric": "anomaly", "task_id": tid,
        "params": {"metric_field": "avail_rate", "k": 3, "min_samples": 20,
                   "min_consecutive": 2, "baseline_days": 14}})
    with caplog.at_level(_lg.WARNING, logger="gpm.alerts"):
        client.post("/api/alerts/evaluate")
    lines = [x for x in caplog.messages if "动态基线预警" in x]
    assert lines, "应产出 5m 快路径预警日志"
    msg = lines[0]
    assert "kσ" not in msg, f"σ 值不得标成 kσ：{msg}"
    assert "σ" in msg and "门槛" in msg, f"须同时报偏离倍数与门槛：{msg}"


def test_baseline_query_zero_params_not_silently_defaulted(tmp_path):
    """显式 0 是有意义的输入（钳到下限），不能被真值门当「未传」静默回落默认。"""
    client, cfg, s = make_client(tmp_path)
    tid = make_task(client)
    seed_history(s, tid)
    r = client.get(f"/api/baseline?task_id={tid}&metric_field=rtt_avg&k=0&min_samples=0")
    assert r.status_code == 200, r.text
    assert r.json()["k"] == 1.5, "k=0 应被钳到下限 1.5（而不是回落默认 3.0）"
