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


# ---------------------------------------------------------------- 词表一致性（防回归）

_SRC = Path(__file__).resolve().parents[2] / "src" / "gpm"

# 允许落到「待定位」的类（应尽量为空；有例外必须写清理由）
_ALLOWED_UNMAPPED: set[str] = set()

_UNKNOWN = ("待定位", "按原始错误与阶段耗时人工判读")


def _emitted_error_classes() -> set[str]:
    """从探测器源码里抽出「可能作为 error_class 产出」的字面量。

    这才是真正的防回归手段：线上 34334 条失败样本里 83% 落到「待定位」，根因就是
    映射表与探测器实际词表脱节；而原来的用例只喂了映射表里**已经有的**类，于是全绿
    但线上无效。这里直接扫源码，探测器新增一个类就会被这个用例拦住。
    """
    import re
    pats = [
        re.compile(r'make_result\([^,]+,\s*[^,]+,\s*"([a-z][a-z0-9_]*)"'),
        re.compile(r'return\s+"(?:fail|ok)",\s*"([a-z][a-z0-9_]*)"'),
        re.compile(r'(?:DnsError|ToolMissing|ToolTimeout)\(\s*"([a-z][a-z0-9_]*)"'),
    ]
    out: set[str] = set()
    for d in (_SRC / "probers", _SRC / "common"):
        for f in sorted(d.glob("*.py")):
            src = f.read_text(encoding="utf-8", errors="replace")
            for p in pats:
                out.update(p.findall(src))
    return {c for c in out if c}


def test_every_prober_error_class_is_classified():
    from gpm.probers.curl import _CURL_EXIT
    classes = _emitted_error_classes() | set(_CURL_EXIT.values())
    missing = sorted(c for c in classes if classify(c) == _UNKNOWN and c not in _ALLOWED_UNMAPPED)
    assert not missing, (
        "这些 error_class 没有分层初判映射，线上会退化成「待定位」：%s\n"
        "新增探测器错误类时请同步补 LAYER_MAP。" % missing)


def test_observed_production_error_classes_are_classified():
    """线上 34334 条失败样本里实际出现过的类（2026-10-03 实测），必须全部有映射。

    修复前：connect_timeout(50.95%)、dns_timeout(26.39%)、path_fail(4.86%) 全是「待定位」。
    """
    for ec in ("connect_timeout", "dns_timeout", "timeout", "path_fail", "tls_error",
               "http_0", "response_timeout", "other"):
        assert classify(ec) != _UNKNOWN, ec


def test_http_status_codes_classified_by_segment():
    """curl 的状态码类是 f"http_{code}" 动态拼的，静态键永远匹配不到具体码。"""
    assert classify("http_0")[0] == "网络层"
    assert classify("http_404")[0] == "应用层"
    assert classify("http_403")[0] == "应用层"
    assert classify("http_503")[0] == "服务端层"
    assert classify("http_500")[0] == "服务端层"
    assert classify("http_301")[0] == "应用层"
    # 形状键仍作兼容命中
    assert classify("http_5xx")[0] == "服务端层"
    assert classify("http_4xx")[0] == "应用层"
    # 非数字尾部不硬猜
    assert classify("http_bad")[0] == "待定位"

