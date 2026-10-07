# -*- coding: utf-8 -*-
"""AI 分析（aiqa）单测：时间解析矩阵 / 锚定校验 / 事实组装 / 降级路径。

变异测试口径（项目惯例）：把被测逻辑还原成修复前行为，对应用例必须红。
"""
from gpm.server import aiqa  # noqa: E402

NOW = 1791400000  # 2026-10-08 03:06:40 本地（固定便于复现）


# ---------------------------------------------------------------- 时间解析

def test_time_pair_hhmm():
    assert aiqa.parse_time_range("14:00~16:00 有哪些故障", NOW) is not None

def test_time_pair_chinese_points_arabic():
    """「14点到16点」阿拉伯点数——首版只写中文数字形式漏解析（回归钉）。"""
    r = aiqa.parse_time_range("昨天 14点到16点 是否有关联", NOW)
    assert r is not None and r[1] > r[0]

def test_time_pair_chinese_points_han():
    assert aiqa.parse_time_range("昨天 十四点到十六点", NOW) is not None

def test_relative_hours():
    f, t = aiqa.parse_time_range("最近 6 小时", NOW)
    assert t == NOW and NOW - f == 6 * 3600

def test_day_before_yesterday_with_hhmm():
    """「前天 9:00-11:00」——日期正则曾误吃时间里的 `00-11` 当月日（回归钉）。"""
    r = aiqa.parse_time_range("前天 9:00-11:00", NOW)
    assert r is not None and r[1] - r[0] == 2 * 3600

def test_iso_datetime():
    assert aiqa.parse_time_range("2026-10-08 09:00~11:30", NOW) is not None

def test_cross_midnight():
    f, t = aiqa.parse_time_range("22:00~02:00 跨零点", NOW)
    assert t - f == 4 * 3600

def test_invalid_hour_rejected():
    """25:00~27:00：非法值必须明说解析不了（返回 None），不猜不抛。"""
    assert aiqa.parse_time_range("25:00~27:00 非法", NOW) is None

def test_unparseable_returns_none():
    assert aiqa.parse_time_range("帮我看看情况", NOW) is None

def test_month_day_form():
    assert aiqa.parse_time_range("10月8日 14点到15点", NOW) is not None


# ---------------------------------------------------------------- 事实组装

def _sample_corr():
    return {
        "window": {"from": 1, "to": 2},
        "total": 3,
        "incidents": [
            {"title": "JD 站点监控 · 默认线路 探测失败", "kind": "probe",
             "started_at": 1791439200, "ended_at": None, "error_class": "timeout"},
            {"title": "节点 win-01 离线", "kind": "node",
             "started_at": 1791440000, "ended_at": 1791440500},
        ],
        "external_alerts": [{"source": "grafana", "title": "CPU 高", "started_at": 1791441000}],
        "clusters": [{"size": 2, "members": [{"title": "JD 站点监控"}, {"title": "Check GitHub"}],
                      "hypotheses": [{"name": "同节点", "hits": 2, "of": 2}]}],
    }


def test_build_facts_brief_and_truncate():
    corr = _sample_corr()
    corr["incidents"] = corr["incidents"] * 40          # 80 条 → max_facts=5 截断
    facts = aiqa.build_facts(corr, max_facts=5)
    assert len(facts["incidents"]) == 5
    assert facts["incidents"][0]["title"]               # brief 只保留白名单键
    assert set(facts["incidents"][0]) <= {"title", "kind", "started_at", "ended_at", "error_class"}


# ---------------------------------------------------------------- 锚定校验

def test_anchor_normal_answer_passes():
    facts = aiqa.build_facts(_sample_corr(), 60)
    ans = "JD 站点监控 在 14 点故障，疑似同节点（2/2 命中），Check GitHub 同批恢复。"
    assert aiqa.anchor_check(ans, facts) == []


def test_anchor_hallucinated_entity_flagged():
    """幻觉实体（事实里不存在的域名）必须被检出——锚定校验的存在意义。"""
    facts = aiqa.build_facts(_sample_corr(), 60)
    unknown = aiqa.anchor_check("淘宝网 www.taobao.com 也出现了故障", facts)
    assert any("taobao" in u for u in unknown)


def test_anchor_rewrite_phrases_not_flagged():
    """改写用语（点故障/也故障了）不该被当实体误伤（首版过严的回归钉）。"""
    facts = aiqa.build_facts(_sample_corr(), 60)
    assert aiqa.anchor_check("JD 站点监控在 14 点故障，疑似同节点", facts) == []


# ---------------------------------------------------------------- 网关形态

def test_ai_enabled_requires_url_and_model():
    assert not aiqa.ai_enabled({})
    assert not aiqa.ai_enabled({"enabled": True, "url": "https://x"})
    assert aiqa.ai_enabled({"enabled": True, "url": "https://x", "model": "m"})
