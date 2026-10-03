"""tests/test_retention_ext.py —— 保留策略扩展：alerts / audit_log / notify_outbox / incidents 清理。

storage.retention() 新参数带默认值（旧六参调用兼容），且只清「过期 + 已终态/已关闭」的数据：

1. alerts / audit_log 按 ts 过期删除，新的保留；
2. notify_outbox 只删 status IN (done, failed) 且时间过期的（done_at 缺省退回 ts），pending 绝不动；
3. incidents 只删已关闭（ended_at 非空）且恢复时间过期的，未关闭的绝不动；
4. 返回值带各表删除行数；days<=0 表示该表跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpm.server.storage import Storage

DAY = 86400
NOW = 1_800_000_000  # 固定「当前时间」，不依赖真实时钟


def make_storage(tmp_path) -> Storage:
    return Storage(str(tmp_path / "retention-ext.db"))


def _counts(s, table: str) -> int:
    with s.lock:
        return s.db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def test_alerts_and_audit_log_purged_by_ts(tmp_path):
    s = make_storage(tmp_path)
    with s.lock:
        s.db.execute("INSERT INTO alerts(ts) VALUES(?)", (NOW - 40 * DAY,))
        s.db.execute("INSERT INTO alerts(ts) VALUES(?)", (NOW - 1 * DAY,))
        s.db.execute("INSERT INTO audit_log(ts,who,action,target) VALUES(?,?,?,?)",
                     (NOW - 40 * DAY, "本机", "新建任务", "任务"))
        s.db.execute("INSERT INTO audit_log(ts,who,action,target) VALUES(?,?,?,?)",
                     (NOW - 1 * DAY, "本机", "修改任务", "任务"))
        s.db.commit()

    out = s.retention(30, 90, 180, 730, 7, NOW, alerts_days=30, audit_days=30)

    assert out["alerts"] == 1 and out["audit_log"] == 1
    assert _counts(s, "alerts") == 1, "过期的告警应删除，新的保留"
    assert _counts(s, "audit_log") == 1, "过期的审计应删除，新的保留"


def test_notify_outbox_only_terminal_rows_purged(tmp_path):
    s = make_storage(tmp_path)
    with s.lock:
        # done 且 done_at 过期 -> 删；done 但 done_at 很新 -> 留
        s.db.execute("INSERT INTO notify_outbox(ts,status,done_at) VALUES(?,'done',?)",
                     (NOW - 10 * DAY, NOW - 10 * DAY))
        s.db.execute("INSERT INTO notify_outbox(ts,status,done_at) VALUES(?,'done',?)",
                     (NOW - 10 * DAY, NOW - 1 * DAY))
        # failed 且 done_at=0（老数据）-> 退回按 ts 判定 -> 删
        s.db.execute("INSERT INTO notify_outbox(ts,status,done_at) VALUES(?,'failed',0)",
                     (NOW - 10 * DAY,))
        # pending 永不清（还没重投完）
        s.db.execute("INSERT INTO notify_outbox(ts,status,done_at) VALUES(?,'pending',0)",
                     (NOW - 10 * DAY,))
        s.db.commit()

    out = s.retention(30, 90, 180, 730, 7, NOW, outbox_days=7)

    assert out["notify_outbox"] == 2
    rows = [dict(r) for r in s.db.execute("SELECT status, done_at FROM notify_outbox")]
    assert [r["status"] for r in rows] == ["done", "pending"], "新 done 与 pending 应保留"


def test_incidents_only_closed_and_expired_purged(tmp_path):
    s = make_storage(tmp_path)
    with s.lock:
        # 已关闭且恢复时间过期 -> 删
        s.db.execute("INSERT INTO incidents(started_at,ended_at) VALUES(?,?)",
                     (NOW - 200 * DAY, NOW - 200 * DAY))
        # 已关闭但还在保留期内 -> 留
        s.db.execute("INSERT INTO incidents(started_at,ended_at) VALUES(?,?)",
                     (NOW - 1 * DAY, NOW - 1 * DAY))
        # 未关闭（ended_at NULL）哪怕再老也绝不动
        s.db.execute("INSERT INTO incidents(started_at,ended_at) VALUES(?,NULL)",
                     (NOW - 365 * DAY,))
        s.db.commit()

    out = s.retention(30, 90, 180, 730, 7, NOW, incidents_days=180)

    assert out["incidents"] == 1
    assert _counts(s, "incidents") == 2, "保留期内与未关闭的事件应保留"


def test_old_six_arg_call_still_works_and_applies_defaults(tmp_path):
    s = make_storage(tmp_path)
    with s.lock:
        s.db.execute("INSERT INTO alerts(ts) VALUES(?)", (NOW - 40 * DAY,))
        s.db.execute("INSERT INTO audit_log(ts) VALUES(?)", (NOW - 40 * DAY,))
        s.db.execute("INSERT INTO notify_outbox(ts,status,done_at) VALUES(?,'done',?)",
                     (NOW - 10 * DAY, NOW - 10 * DAY))
        s.db.execute("INSERT INTO incidents(started_at,ended_at) VALUES(?,?)",
                     (NOW - 200 * DAY, NOW - 200 * DAY))
        s.db.commit()

    # 旧签名调用（app.py 历史形态）：新表按默认值（30/30/7/180）清理
    out = s.retention(30, 90, 180, 730, 7, NOW)

    # external_* 是第六期新增的表，旧签名调用也要按默认值清理（此处没有数据 → 0）
    assert out == {"alerts": 1, "audit_log": 1, "notify_outbox": 1, "incidents": 1,
                   "external_alert_links": 0, "external_alerts": 0}
    assert _counts(s, "alerts") == 0 and _counts(s, "incidents") == 0


def test_external_alerts_purged_with_their_links(tmp_path):
    """第三方告警是「提示」不是证据：按最后活动时间清理，且先删关联再删主表
    （否则会留下指向不存在告警的悬空旁证）。未恢复的也按最后活动时间走 —— 提示型数据
    留太久只会占地方。"""
    s = make_storage(tmp_path)
    with s.lock:
        # 老的 firing（无 ended_at，靠 started_at 判定）+ 关联
        s.db.execute("INSERT INTO external_alerts(id,source,source_id,status,started_at,"
                     "received_at) VALUES(1,'grafana','old','firing',?,?)",
                     (NOW - 40 * DAY, NOW - 40 * DAY))
        s.db.execute("INSERT INTO external_alert_links(alert_id,incident_id) VALUES(1, 7)")
        # 新的 resolved → 按 ended_at 判定，保留
        s.db.execute("INSERT INTO external_alerts(id,source,source_id,status,started_at,"
                     "ended_at,received_at) VALUES(2,'zabbix','new','resolved',?,?,?)",
                     (NOW - 2 * DAY, NOW - 1 * DAY, NOW - 1 * DAY))
        s.db.execute("INSERT INTO external_alert_links(alert_id,incident_id) VALUES(2, 8)")
        s.db.commit()

    out = s.retention(30, 90, 180, 730, 7, NOW, external_days=30)

    assert out["external_alerts"] == 1 and out["external_alert_links"] == 1, out
    assert _counts(s, "external_alerts") == 1 and _counts(s, "external_alert_links") == 1
    with s.lock:
        left = [r["id"] for r in s.db.execute("SELECT id FROM external_alerts")]
    assert left == [2], left


def test_non_positive_days_skip_table(tmp_path):
    s = make_storage(tmp_path)
    with s.lock:
        s.db.execute("INSERT INTO alerts(ts) VALUES(?)", (NOW - 40 * DAY,))
        s.db.execute("INSERT INTO audit_log(ts) VALUES(?)", (NOW - 40 * DAY,))
        s.db.execute("INSERT INTO incidents(started_at,ended_at) VALUES(?,?)",
                     (NOW - 200 * DAY, NOW - 200 * DAY))
        s.db.commit()

    out = s.retention(30, 90, 180, 730, 7, NOW, alerts_days=0, audit_days=0,
                      outbox_days=0, incidents_days=0, external_days=0)

    assert out == {}, "days<=0 的表应整体跳过（不出现键）"
    assert _counts(s, "alerts") == 1 and _counts(s, "audit_log") == 1
    assert _counts(s, "incidents") == 1
