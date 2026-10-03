"""diagnose 纯函数单测：error_class 分层初判（classify）+ 范围三档判定（verdict）。

覆盖：映射表代表性条目、未知/空类别回落「待定位」、verdict 三档
（all_nodes / partial / single_node）与空态（无失败样本 / 无数据 / skipped 不计分母）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server.diagnose import LAYER_MAP, classify, verdict  # noqa: E402


# ---------------------------------------------------------------- classify

def test_classify_representative_layers():
    assert classify("dns_error")[0] == "DNS 层"
    assert "dns 任务逐线路表" in classify("dns_error")[1]
    assert classify("timeout")[0] == "网络层"
    assert classify("refused")[0] == "网络/端口层"
    assert classify("tls_error")[0] == "TLS 层"
    assert classify("cert_expired")[0] == "TLS 层"
    assert classify("http_5xx")[0] == "服务端层"
    assert classify("keyword_miss")[0] == "应用层"
    assert classify("regex_miss")[0] == "应用层"
    assert classify("tool_missing")[0] == "节点侧"
    assert classify("fake_ip")[0] == "节点侧(DNS)"


def test_classify_unknown_and_blank_fall_back():
    for bad in ("", "  ", "some_new_error", "HTTP_5XX"):
        layer, advice = classify(bad)
        assert layer == "待定位", bad
        assert advice, bad
    # 大小写敏感：映射表按原文匹配，未知大写不算命中
    assert "HTTP_5XX" not in LAYER_MAP


def test_classify_never_raises():
    assert classify("x" * 500) == ("待定位", "按原始错误与阶段耗时人工判读")


# ---------------------------------------------------------------- verdict

def _states(*items):
    """items: (name, status)；status 缺省 ok。"""
    return [{"node_name": n, "status": s} for n, s in items]


def test_verdict_all_nodes_failed():
    v = verdict(_states(("北京", "fail"), ("上海", "fail"), ("广州", "fail")))
    assert v["mode"] == "all_nodes"
    assert v["failed"] == 3 and v["total"] == 3
    assert v["verdict"].startswith("全节点失败")
    assert v["advice"]
    assert v["failed_names"] == ["北京", "上海", "广州"]


def test_verdict_single_node_failed():
    v = verdict(_states(("北京", "fail"), ("上海", "ok"), ("广州", "ok")))
    assert v["mode"] == "single_node"
    assert v["failed"] == 1 and v["total"] == 3
    assert v["verdict"].startswith("仅单节点失败")
    assert v["failed_names"] == ["北京"]


def test_verdict_partial_failed():
    v = verdict(_states(("北京", "fail"), ("上海", "fail"), ("广州", "ok")))
    assert v["mode"] == "partial"
    assert v["failed"] == 2 and v["total"] == 3
    assert v["verdict"].startswith("部分节点失败")


def test_verdict_empty_states_no_conclusion():
    v = verdict([])
    assert v["mode"] == "" and v["failed"] == 0 and v["total"] == 0
    assert v["verdict"] == "" and v["advice"] == "" and v["failed_names"] == []


def test_verdict_no_failure_no_conclusion():
    v = verdict(_states(("北京", "ok"), ("上海", "ok")))
    assert v["mode"] == "" and v["total"] == 2 and v["failed"] == 0


def test_verdict_single_node_total_is_partial():
    # 只有一个节点且失败：不满足「>1 才算全节点」，归 partial（单节点也有结论）
    v = verdict(_states(("北京", "fail")))
    assert v["mode"] == "partial"
    assert v["failed"] == 1 and v["total"] == 1


def test_verdict_skipped_excluded_from_denominator():
    v = verdict(_states(("北京", "fail"), ("上海", "ok"), ("广州", "skipped")))
    assert v["mode"] == "single_node"
    assert v["total"] == 2 and v["failed"] == 1
    # 全部 skipped → 无分母，无结论
    v2 = verdict(_states(("北京", "skipped"), ("上海", "skipped")))
    assert v2["mode"] == "" and v2["total"] == 0


def test_verdict_tolerates_dirty_rows():
    v = verdict([{"node_name": "", "status": "fail"},
                 {"node_id": "n9", "status": "fail"},
                 {"node_name": "x", "status": "unknown"},
                 {"node_name": "y"}])
    assert v["mode"] == "all_nodes"
    assert "" in v["failed_names"] and "n9" in v["failed_names"]
