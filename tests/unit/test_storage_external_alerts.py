"""storage 第三方告警家族（external_alert_*）单测（第六期）：

- external_alert_upsert：幂等写入（同 source+source_id 更新同一行、received_at 保持
  首次接收时刻、updated_at 刷新）、不同 source / source_id 各自成行、缺参报错；
- external_alert_get：存在（labels / raw 反序列化回 dict）/ 不存在（None）；
- list_external_alerts：limit、source / status / firing_only 过滤、t_from 时间下界
  （started_at=0 退回 received_at）、按生效时刻倒序（新的在前）；
- external_alert_link / external_alert_links_for：旁证关联幂等（重复关联返回 False
  且不覆盖原 reason）、无关联的 incident 不出现在映射里、空列表返回 {}；
- external_alert_stats：窗口内按来源计 firing / resolved、平均持续时间（仅
  ended_at>started_at 的已恢复事件）、窗口边界含、窗口外不计、空窗返回结构；
- external_alert_summary：days 天窗口（cutoff=ts-days*86400，边界含）、窗口外不计、
  days=0 全量、按 source 排序的结构。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from gpm.server.storage import Storage


def make_storage(tmp_path):
    s = Storage(str(tmp_path / "ext_alerts.db"))
    return s, 1_700_000_000


def _fields(**kw):
    f = {"title": "CPU 高", "severity": "critical", "status": "firing",
         "started_at": 1_700_000_000, "ended_at": 0,
         "labels": {"alertname": "HighCPU", "instance": "node-1"},
         "url": "https://grafana.example/alert/1", "raw": {"rule": "cpu>90"}}
    f.update(kw)
    return f


# ---------------- external_alert_upsert ----------------

def test_upsert_insert_then_get(tmp_path):
    s, t0 = make_storage(tmp_path)
    aid, created = s.external_alert_upsert("grafana", "g-1", _fields(), t0)
    assert created is True and aid > 0
    row = s.external_alert_get(aid)
    assert row["id"] == aid
    assert row["source"] == "grafana" and row["source_id"] == "g-1"
    assert row["title"] == "CPU 高" and row["severity"] == "critical"
    assert row["status"] == "firing" and row["started_at"] == t0 and row["ended_at"] == 0
    assert row["labels"] == {"alertname": "HighCPU", "instance": "node-1"}
    assert row["raw"] == {"rule": "cpu>90"}
    assert row["url"] == "https://grafana.example/alert/1"
    assert row["received_at"] == t0 and row["updated_at"] == t0


def test_upsert_idempotent_update_same_row(tmp_path):
    s, t0 = make_storage(tmp_path)
    aid1, created1 = s.external_alert_upsert("grafana", "g-1", _fields(), t0)
    aid2, created2 = s.external_alert_upsert(
        "grafana", "g-1",
        _fields(title="CPU 高（恢复）", status="resolved", ended_at=t0 + 600,
                labels={"alertname": "HighCPU", "state": "ok"}),
        t0 + 600)
    assert created1 is True and created2 is False
    assert aid2 == aid1                                   # 同一行：id 不变
    assert len(s.list_external_alerts()) == 1             # 行数不增加
    row = s.external_alert_get(aid1)
    assert row["title"] == "CPU 高（恢复）" and row["status"] == "resolved"
    assert row["ended_at"] == t0 + 600
    assert row["labels"] == {"alertname": "HighCPU", "state": "ok"}
    # received_at 保持首次接收时刻，updated_at 刷新为最后一次写入
    assert row["received_at"] == t0 and row["updated_at"] == t0 + 600


def test_upsert_separate_rows_for_different_source_or_id(tmp_path):
    s, t0 = make_storage(tmp_path)
    a1, c1 = s.external_alert_upsert("grafana", "g-1", _fields(), t0)
    a2, c2 = s.external_alert_upsert("zabbix", "g-1",
                                     _fields(title="Zabbix 同 source_id"), t0)
    a3, c3 = s.external_alert_upsert("grafana", "g-2",
                                     _fields(title="同源不同 source_id"), t0)
    assert len({a1, a2, a3}) == 3 and c1 and c2 and c3    # 各自成行
    assert s.external_alert_get(a2)["source"] == "zabbix"
    assert s.external_alert_get(a2)["title"] == "Zabbix 同 source_id"
    assert s.external_alert_get(a3)["source"] == "grafana"
    assert s.external_alert_get(a3)["source_id"] == "g-2"
    assert {r["id"] for r in s.list_external_alerts()} == {a1, a2, a3}


def test_upsert_requires_source_and_source_id(tmp_path):
    s, t0 = make_storage(tmp_path)
    with pytest.raises(ValueError):
        s.external_alert_upsert("", "g-1", _fields(), t0)
    with pytest.raises(ValueError):
        s.external_alert_upsert("grafana", "", _fields(), t0)


# ---------------- external_alert_get ----------------

def test_get_missing_returns_none(tmp_path):
    s, t0 = make_storage(tmp_path)
    assert s.external_alert_get(99999) is None
    aid, _ = s.external_alert_upsert("grafana", "g-1", _fields(), t0)
    assert s.external_alert_get(aid)["id"] == aid
    assert s.external_alert_get(aid + 999) is None


# ---------------- list_external_alerts ----------------

def test_list_filters_and_ordering(tmp_path):
    s, t0 = make_storage(tmp_path)
    g_old = s.external_alert_upsert("grafana", "g1", _fields(started_at=t0), t0)[0]
    g_new = s.external_alert_upsert(
        "grafana", "g2",
        _fields(started_at=t0 + 100, status="resolved", ended_at=t0 + 150), t0 + 100)[0]
    z1 = s.external_alert_upsert("zabbix", "z1", _fields(started_at=t0 + 50), t0 + 50)[0]
    # started_at=0 的告警：生效时刻退回 received_at
    tc1 = s.external_alert_upsert("tencent", "tc1",
                                  _fields(started_at=0, title="无起始时刻"), t0 + 300)[0]

    # 排序：按生效时刻（started_at，为 0 时退回 received_at）倒序，新的在前
    assert [r["id"] for r in s.list_external_alerts()] == [tc1, g_new, z1, g_old]
    assert [r["id"] for r in s.list_external_alerts(limit=2)] == [tc1, g_new]
    # source / status / firing_only 过滤
    assert {r["id"] for r in s.list_external_alerts(source="grafana")} == {g_old, g_new}
    assert {r["id"] for r in s.list_external_alerts(status="resolved")} == {g_new}
    assert {r["id"] for r in s.list_external_alerts(firing_only=True)} == {g_old, z1, tc1}
    # 组合过滤：source + firing_only
    assert {r["id"] for r in s.list_external_alerts(
        source="grafana", firing_only=True)} == {g_old}
    # t_from 时间下界（边界含：g_old 的 started_at=t0 被排除是因 < t0+60）
    assert {r["id"] for r in s.list_external_alerts(t_from=t0 + 60)} == {tc1, g_new}
    assert {r["id"] for r in s.list_external_alerts(t_from=t0 + 301)} == set()


# ---------------- external_alert_link / links_for ----------------

def test_link_idempotent_and_links_for_mapping(tmp_path):
    s, t0 = make_storage(tmp_path)
    aid, _ = s.external_alert_upsert("grafana", "g1", _fields(), t0)
    inc1 = s.incident_open("t1", "n1", "", "https://x", t0, {"r": 1})
    inc2 = s.incident_open("t1", "n2", "", "https://y", t0, {"r": 2})

    assert s.external_alert_link(aid, inc1, t0 + 10, reason="目标+时间窗匹配") is True
    # 重复关联幂等：返回 False，不新增行、不覆盖原 reason
    assert s.external_alert_link(aid, inc1, t0 + 20, reason="重复关联") is False

    links = s.external_alert_links_for([inc1, inc2])
    assert set(links.keys()) == {inc1}                    # 无关联的 incident 不出现键
    assert len(links[inc1]) == 1
    lk = links[inc1][0]
    assert lk["id"] == aid and lk["source"] == "grafana"
    assert lk["reason"] == "目标+时间窗匹配"
    assert lk["labels"] == {"alertname": "HighCPU", "instance": "node-1"}  # 已反序列化

    # 多 incident 各自映射；不存在的 incident 不出现
    aid2, _ = s.external_alert_upsert("zabbix", "z1", _fields(title="Z"), t0)
    assert s.external_alert_link(aid2, inc2, t0 + 30, reason="r2") is True
    links2 = s.external_alert_links_for([inc1, inc2, 999_999])
    assert set(links2.keys()) == {inc1, inc2}
    assert links2[inc1][0]["id"] == aid and links2[inc2][0]["id"] == aid2
    # 空 incident_ids → 空 dict
    assert s.external_alert_links_for([]) == {}


# ---------------- external_alert_stats ----------------

def test_stats_window_sources_and_duration(tmp_path):
    s, t0 = make_storage(tmp_path)
    # 窗口 [t0, t0+3600] 内：grafana 1 firing + 1 resolved（持续 300s），zabbix 2 条
    s.external_alert_upsert("grafana", "g1", _fields(
        started_at=t0, status="resolved", ended_at=t0 + 300), t0)
    s.external_alert_upsert("grafana", "g2", _fields(started_at=t0 + 100), t0 + 100)
    s.external_alert_upsert("zabbix", "z1", _fields(
        started_at=t0 + 200, status="resolved", ended_at=t0 + 600), t0 + 200)
    s.external_alert_upsert("zabbix", "z2", _fields(started_at=t0 + 3600), t0 + 3600)
    # 窗口外：早于 t_from / 晚于 t_to 各一条，不计入
    s.external_alert_upsert("grafana", "g3", _fields(started_at=t0 - 1), t0 - 1)
    s.external_alert_upsert("grafana", "g4", _fields(started_at=t0 + 3601), t0 + 3601)

    stats = s.external_alert_stats(t0, t0 + 3600)
    assert stats["mtta_note"]                             # 说明文案在
    by = {b["source"]: b for b in stats["sources"]}
    assert set(by) == {"grafana", "zabbix"}               # 窗口外的条目不产生来源行
    assert by["grafana"]["total"] == 2
    assert by["grafana"]["firing"] == 1 and by["grafana"]["resolved"] == 1
    assert by["grafana"]["avg_duration_s"] == 300.0
    assert by["zabbix"]["total"] == 2                     # t0+3600 边界含
    assert by["zabbix"]["firing"] == 1 and by["zabbix"]["resolved"] == 1
    assert by["zabbix"]["avg_duration_s"] == 400.0
    # sources 按 source 名排序
    assert [b["source"] for b in stats["sources"]] == ["grafana", "zabbix"]


def test_stats_empty_window_structure(tmp_path):
    s, t0 = make_storage(tmp_path)
    stats = s.external_alert_stats(t0, t0 + 3600)
    assert stats["sources"] == [] and stats["mtta_note"]
    s.external_alert_upsert("grafana", "g1", _fields(started_at=t0), t0)
    assert s.external_alert_stats(t0 + 7200, t0 + 10800)["sources"] == []


def test_stats_avg_duration_ignores_nonpositive_intervals(tmp_path):
    s, t0 = make_storage(tmp_path)
    # 已恢复但 ended_at=0（缺结束时刻）→ 不进平均持续时间的样本
    s.external_alert_upsert("gcp", "c1", _fields(
        started_at=t0, status="resolved", ended_at=0), t0)
    by = {b["source"]: b for b in s.external_alert_stats(t0, t0 + 60)["sources"]}
    assert by["gcp"]["resolved"] == 1
    assert by["gcp"]["avg_duration_s"] is None


# ---------------- external_alert_summary ----------------

def test_summary_daily_window_and_structure(tmp_path):
    s, t0 = make_storage(tmp_path)
    d = 86400
    # t0+2d 附近：grafana 1 firing + 1 resolved、zabbix 1 firing
    s.external_alert_upsert("grafana", "g1", _fields(
        started_at=t0 + 2 * d, status="firing"), t0 + 2 * d)
    s.external_alert_upsert("grafana", "g2", _fields(
        started_at=t0 + 2 * d + 60, status="resolved", ended_at=t0 + 2 * d + 120),
        t0 + 2 * d + 60)
    s.external_alert_upsert("zabbix", "z1", _fields(started_at=t0 + 2 * d + 30),
                            t0 + 2 * d + 30)
    # 更早一条（t0+d-1），在 days=2 窗口（cutoff=t0+d）之外
    s.external_alert_upsert("grafana", "g0", _fields(started_at=t0 + d - 1), t0 + d - 1)

    sm = s.external_alert_summary(t0 + 3 * d, days=2)
    assert sm["days"] == 2
    by = {b["source"]: b for b in sm["sources"]}
    assert set(by) == {"grafana", "zabbix"}               # 窗口外的 g0 不计入
    assert by["grafana"]["total"] == 2
    assert by["grafana"]["firing"] == 1 and by["grafana"]["resolved"] == 1
    assert by["zabbix"] == {"source": "zabbix", "firing": 1, "resolved": 0, "total": 1}
    # sources 按 source 名排序
    assert [b["source"] for b in sm["sources"]] == ["grafana", "zabbix"]

    # 边界含：恰好落在 cutoff（t0+d）的告警计入
    s.external_alert_upsert("tencent", "tc1", _fields(started_at=t0 + d), t0 + d)
    by2 = {b["source"]: b for b in s.external_alert_summary(t0 + 3 * d, days=2)["sources"]}
    assert by2["tencent"]["total"] == 1

    # days=0 → 全量（含窗口外的 g0）
    sm_all = s.external_alert_summary(t0 + 3 * d, days=0)
    assert sm_all["days"] == 0
    by_all = {b["source"]: b for b in sm_all["sources"]}
    assert by_all["grafana"]["total"] == 3
