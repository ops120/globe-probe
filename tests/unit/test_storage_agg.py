"""storage 聚合域单测（agg_recompute / agg_read / agg_buckets_existing + 新索引回归）：

- 索引回归（第八期第 1 项）：aggregates 表必须有 ix_agg_bucket_task_ts /
  ix_agg_bucket_node_ts（按 task/node 范围查时避免只走主键左部导致全表扫）；
- agg_recompute 1m：从 probe_results 现算——skipped 不计入 count/ok/fail/http_code、
  rtt_avg 为 round(mean,2)、rtt_p50/rtt_p95 按实现 pct() 公式（nearest-rank 式）取值、
  rtt_max、loss_rate 只对有值样本求均值（round 4 位）、avail_rate=ok/n（round 4 位）、
  http_code_json 按状态码计数；无 rtt 样本时 avg/p50/p95/max 全 None（不能编 0）；
- 幂等：同桶区间重跑两次，行数与值都不变（先删后插 + INSERT OR REPLACE）；
- 区间裁剪：[b_from, b_to) 之外的 probe_results 不参与重算；
- 高阶桶：5m 从 1m 派生——count/ok 求和、rtt_avg 按样本数加权、rtt_p95/rtt_max
  取跨子桶最大、http_code_json 跨子桶合并、avail_rate=Σok/Σcount；
  再验一条 1h←5m 链路（含 None 子桶不参与加权）；
- agg_buckets_existing：任务级跨流汇总（count/ok/fail 求和、rtt_avg 跨流平均、
  rtt_p95 取 max、avail 按 Σok/Σcount 重算），无数据/未算过/窗口外返回空表；
- agg_read：按 (bucket, task_id, node_id, dns, url) 精确读回，区间为双端闭。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from gpm.server.storage import BUCKET_SECONDS, Storage

SEC = BUCKET_SECONDS
# 固定基准时刻：1_700_000_000 向下对齐到日桶（86400 的整数倍必然同时是
# 3600/300/60 的整数倍），所有桶边界都从 BUCKET_SECONDS 推导，不写魔数除法
T0 = 1_700_000_000 // SEC["1d"] * SEC["1d"]
URL = "https://example.com"


def make_storage(tmp_path, nodes=("北京",)):
    s = Storage(str(tmp_path / "agg.db"))
    s.create_task("t1", "curl-目标", "curl", URL, [URL], {}, [], 30, T0)
    ids = {}
    for name in nodes:
        nid, _ = s.register_node(name, "h", {}, "v1", {}, T0)
        ids[name] = nid
    return s, ids


def _row(ts, node_id, status, metrics=None, task_id="t1", dns="", url=URL):
    return {"ts": ts, "task_id": task_id, "node_id": node_id, "type": "curl",
            "dns": dns, "url": url, "status": status,
            "error_class": "" if status == "ok" else "timeout",
            "error": "" if status == "ok" else "boom",
            "metrics": metrics if metrics is not None else {}}


def _agg_row(s, bucket, node_id, ts, task_id="t1", dns="", url=URL):
    """精确读回单个聚合桶；不存在时返回 None。"""
    rows = s.agg_read(bucket, task_id, node_id, dns, url, ts, ts)
    return rows[0] if rows else None


# ---------------- 索引回归（第八期第 1 项） ----------------

def test_agg_indexes_exist(tmp_path):
    s = Storage(str(tmp_path / "agg-idx.db"))
    names = {r["name"] for r in s.db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='aggregates'")}
    # 早期漏建：按 (bucket, task_id/node_id, ts) 范围查聚合时只能走主键左部
    assert "ix_agg_bucket_task_ts" in names
    assert "ix_agg_bucket_node_ts" in names


# ---------------- agg_recompute：1m 从 probe_results 现算 ----------------

def test_agg_recompute_1m_mixed(tmp_path):
    s, ids = make_storage(tmp_path)
    nid = ids["北京"]
    s.insert_results([
        _row(T0 + 1, nid, "ok", {"rtt_avg": 10.0, "loss_rate": 0.0, "http_code": 200}),
        _row(T0 + 2, nid, "ok", {"rtt_avg": 20.0, "http_code": 200}),    # 无 loss 值
        _row(T0 + 3, nid, "ok", {"rtt_avg": 30.0, "loss_rate": 0.5, "http_code": 200}),
        _row(T0 + 4, nid, "fail", {"rtt_avg": None, "loss_rate": 1.0, "http_code": 500}),
        _row(T0 + 5, nid, "fail", {"loss_rate": 1.0}),                   # 无 rtt 键
        _row(T0 + 6, nid, "skipped",
             {"rtt_avg": 999.0, "loss_rate": 9.0, "http_code": 200}),
    ], T0 + 6)
    s.agg_recompute("1m", T0, T0 + SEC["1m"])
    r = _agg_row(s, "1m", nid, T0)
    assert r is not None and r["ts"] == T0
    # skipped 整条排除：count=5 而非 6，ok=3、fail=2
    assert r["count"] == 5 and r["ok"] == 3 and r["fail"] == 2
    # rtts=[10,20,30]：p50=rtts[round(0.5*2)]=20，p95=rtts[round(0.95*2)]=30
    assert r["rtt_avg"] == pytest.approx(20.0)            # round(60/3, 2)
    assert r["rtt_p50"] == pytest.approx(20.0)
    assert r["rtt_p95"] == pytest.approx(30.0)
    assert r["rtt_max"] == pytest.approx(30.0)
    # loss 只对有值样本求均值：(0.0+0.5+1.0+1.0)/4，不含无 loss 的样本与 skipped 的 9.0
    assert r["loss_rate"] == pytest.approx(0.625)
    assert r["avail_rate"] == pytest.approx(0.6)          # round(3/5, 4)
    # skipped 样本的 200 不计数
    assert json.loads(r["http_code_json"]) == {"200": 3, "500": 1}


def test_agg_recompute_1m_without_rtt(tmp_path):
    s, ids = make_storage(tmp_path)
    nid = ids["北京"]
    s.insert_results([
        _row(T0 + 1, nid, "fail", {"loss_rate": 1.0, "http_code": 500}),
        _row(T0 + 2, nid, "fail", {"rtt_avg": None, "loss_rate": 1.0}),
    ], T0 + 2)
    s.agg_recompute("1m", T0, T0 + SEC["1m"])
    r = _agg_row(s, "1m", nid, T0)
    assert r["count"] == 2 and r["ok"] == 0 and r["fail"] == 2
    # 无 rtt 样本：均值/分位/最大值都是 None，不能编成 0
    assert r["rtt_avg"] is None
    assert r["rtt_p50"] is None
    assert r["rtt_p95"] is None
    assert r["rtt_max"] is None
    assert r["loss_rate"] == pytest.approx(1.0)
    assert r["avail_rate"] == pytest.approx(0.0)
    assert json.loads(r["http_code_json"]) == {"500": 1}


def test_agg_recompute_1m_idempotent(tmp_path):
    s, ids = make_storage(tmp_path, nodes=("北京", "上海"))
    na, nb = ids["北京"], ids["上海"]
    s.insert_results([
        _row(T0 + 1, na, "ok", {"rtt_avg": 10.0, "loss_rate": 0.0}),
        _row(T0 + 2, na, "fail", {"rtt_avg": 25.0, "loss_rate": 1.0}),
        _row(T0 + 3, nb, "ok", {"rtt_avg": 30.0, "loss_rate": 0.5}),
        _row(T0 + 4, nb, "skipped", {}),
    ], T0 + 4)

    def snapshot():
        return (s.agg_read("1m", "t1", na, "", URL, T0, T0),
                s.agg_read("1m", "t1", nb, "", URL, T0, T0))

    s.agg_recompute("1m", T0, T0 + SEC["1m"])
    first = snapshot()
    assert len(first[0]) == 1 and len(first[1]) == 1
    s.agg_recompute("1m", T0, T0 + SEC["1m"])          # 同桶区间重跑
    assert snapshot() == first                          # 行数不变、值不变


def test_agg_recompute_1m_interval_clipping(tmp_path):
    s, ids = make_storage(tmp_path)
    nid = ids["北京"]
    s.insert_results([
        _row(T0 + 1, nid, "ok", {"rtt_avg": 10.0}),          # T0 桶
        _row(T0 + 1 * SEC["1m"] + 1, nid, "fail", {}),       # T0+60 桶
        _row(T0 + 2 * SEC["1m"] + 1, nid, "ok", {"rtt_avg": 30.0}),  # T0+120 桶
    ], T0 + 2 * SEC["1m"] + 1)
    # 只重算中间那分钟：[T0+60, T0+120)
    s.agg_recompute("1m", T0 + SEC["1m"], T0 + 2 * SEC["1m"])
    rows = s.agg_read("1m", "t1", nid, "", URL, 0, T0 + 999)
    assert len(rows) == 1 and rows[0]["ts"] == T0 + SEC["1m"]
    # 只卷入本桶样本：两侧的 ok 样本没有把 count/ok 抬高
    assert rows[0]["count"] == 1 and rows[0]["ok"] == 0 and rows[0]["fail"] == 1
    assert _agg_row(s, "1m", nid, T0) is None
    assert _agg_row(s, "1m", nid, T0 + 2 * SEC["1m"]) is None


# ---------------- agg_recompute：高阶桶从上一级桶派生 ----------------

def test_agg_recompute_5m_from_1m(tmp_path):
    s, ids = make_storage(tmp_path)
    nid = ids["北京"]
    s.insert_results([
        # 5m 桶 T0 内的两个 1m 子桶
        _row(T0 + 1, nid, "ok", {"rtt_avg": 10.0, "loss_rate": 0.0, "http_code": 200}),
        _row(T0 + 2, nid, "ok", {"rtt_avg": 20.0, "loss_rate": 0.2, "http_code": 200}),
        _row(T0 + 3, nid, "fail", {"rtt_avg": None, "loss_rate": 1.0, "http_code": 502}),
        _row(T0 + 1 * SEC["1m"] + 1, nid, "ok",
             {"rtt_avg": 30.0, "loss_rate": 0.0, "http_code": 200}),
        _row(T0 + 1 * SEC["1m"] + 2, nid, "fail", {"loss_rate": 1.0, "http_code": 500}),
    ], T0 + 1 * SEC["1m"] + 2)
    s.agg_recompute("1m", T0, T0 + SEC["5m"])
    # 前置：两个 1m 子桶的 rtt_avg 分别是 15（(10+20)/2）与 30
    assert _agg_row(s, "1m", nid, T0)["rtt_avg"] == pytest.approx(15.0)
    assert _agg_row(s, "1m", nid, T0 + SEC["1m"])["rtt_avg"] == pytest.approx(30.0)
    s.agg_recompute("5m", T0, T0 + SEC["5m"])
    rows = s.agg_read("5m", "t1", nid, "", URL, 0, T0 + 999)
    assert len(rows) == 1 and rows[0]["ts"] == T0
    r = rows[0]
    # count/ok 跨子桶求和：3+2、2+1
    assert r["count"] == 5 and r["ok"] == 3 and r["fail"] == 2
    # rtt_avg 按样本数加权：(15*3 + 30*2) / 5 = 21
    assert r["rtt_avg"] == pytest.approx(21.0)
    # 子桶 rtt_avg 的上中位：sorted([15,30])[2//2] = 30
    assert r["rtt_p50"] == pytest.approx(30.0)
    # p95 / max 取跨子桶最大：max(20,30)
    assert r["rtt_p95"] == pytest.approx(30.0)
    assert r["rtt_max"] == pytest.approx(30.0)
    # http_code 跨子桶合并：{"200":2,"502":1} + {"200":1,"500":1}
    assert json.loads(r["http_code_json"]) == {"200": 3, "502": 1, "500": 1}
    assert r["avail_rate"] == pytest.approx(0.6)          # 3/5
    # 子桶 loss 均值的均值：(0.4 + 0.5) / 2
    assert r["loss_rate"] == pytest.approx(0.45)


def test_agg_recompute_1h_chain_from_5m(tmp_path):
    s, ids = make_storage(tmp_path)
    nid = ids["北京"]
    s.insert_results([
        _row(T0 + 1, nid, "ok", {"rtt_avg": 10.0, "http_code": 200}),
        _row(T0 + 1 * SEC["5m"] + 1, nid, "fail", {"http_code": 500}),  # 第二个 5m 桶，无 rtt
    ], T0 + 1 * SEC["5m"] + 1)
    for b in ("1m", "5m", "1h"):                        # 顺序：1m → 5m → 1h
        s.agg_recompute(b, T0, T0 + SEC["1h"])
    r = _agg_row(s, "1h", nid, T0)
    assert r is not None and _agg_row(s, "1h", nid, T0 + SEC["1h"]) is None
    assert r["count"] == 2 and r["ok"] == 1 and r["fail"] == 1
    # 无 rtt 的子桶不参与加权：均值仍由有值子桶给出
    assert r["rtt_avg"] == pytest.approx(10.0)
    assert r["rtt_p50"] == pytest.approx(10.0)
    assert r["rtt_p95"] == pytest.approx(10.0)
    assert r["rtt_max"] == pytest.approx(10.0)
    assert r["avail_rate"] == pytest.approx(0.5)
    assert r["loss_rate"] is None                        # 全链路都没有 loss 值
    assert json.loads(r["http_code_json"]) == {"200": 1, "500": 1}


# ---------------- agg_buckets_existing：任务级已算桶汇总 ----------------

def test_agg_buckets_existing(tmp_path):
    s, ids = make_storage(tmp_path, nodes=("北京", "上海"))
    na, nb = ids["北京"], ids["上海"]
    s.insert_results([
        _row(T0 + 1, na, "ok", {"rtt_avg": 10.0, "loss_rate": 0.0}),
        _row(T0 + 2, nb, "ok", {"rtt_avg": 30.0, "loss_rate": 0.0}),
        _row(T0 + 3, nb, "fail", {"loss_rate": 1.0}),
        _row(T0 + 1 * SEC["1m"] + 1, na, "fail", {"loss_rate": 1.0}),  # 第二分钟，无 rtt
    ], T0 + 1 * SEC["1m"] + 1)
    s.agg_recompute("1m", T0, T0 + 2 * SEC["1m"])
    rows = s.agg_buckets_existing("1m", "t1", T0, T0 + 2 * SEC["1m"])
    assert [r["ts"] for r in rows] == [T0, T0 + SEC["1m"]]
    b0, b1 = rows
    assert b0["count"] == 3 and b0["ok"] == 2 and b0["fail"] == 1
    assert b0["rtt_avg"] == pytest.approx(20.0)           # AVG(10, 30) 跨流平均
    assert b0["rtt_p95"] == pytest.approx(30.0)           # MAX 跨流
    assert b0["avail_rate"] == pytest.approx(2 / 3)       # Σok/Σcount 重算，非各流均值
    assert b0["loss_rate"] == pytest.approx(0.25)         # AVG(0.0, 0.5)
    assert b1["count"] == 1 and b1["ok"] == 0 and b1["fail"] == 1
    assert b1["rtt_avg"] is None and b1["rtt_p95"] is None  # 该桶所有流都无 rtt
    assert b1["avail_rate"] == pytest.approx(0.0)
    assert b1["loss_rate"] == pytest.approx(1.0)
    # 区间为双端闭：上界正好压在桶起点时该桶仍计入
    assert len(s.agg_buckets_existing("1m", "t1", T0, T0)) == 1
    # 无数据 → 空表：任务不存在 / 桶未算过 / 窗口在数据之前
    assert s.agg_buckets_existing("1m", "t-none", T0, T0 + SEC["1m"]) == []
    assert s.agg_buckets_existing("5m", "t1", T0, T0 + SEC["1m"]) == []
    assert s.agg_buckets_existing("1m", "t1", T0 - 10 * SEC["1m"], T0 - 5 * SEC["1m"]) == []


# ---------------- agg_read：四元组精确读回与区间过滤 ----------------

def test_agg_read_dimensions_and_window(tmp_path):
    s, ids = make_storage(tmp_path, nodes=("北京", "上海"))
    na, nb = ids["北京"], ids["上海"]
    s.insert_results([
        _row(T0 + 1, na, "ok", {"rtt_avg": 10.0}),
        _row(T0 + 2, na, "fail", {}),
        _row(T0 + 3, nb, "ok", {"rtt_avg": 30.0}),
        _row(T0 + 1 * SEC["1m"] + 1, na, "ok", {"rtt_avg": 20.0}),  # 第二分钟
    ], T0 + 1 * SEC["1m"] + 1)
    s.agg_recompute("1m", T0, T0 + 2 * SEC["1m"])
    rows = s.agg_read("1m", "t1", na, "", URL, T0, T0 + 2 * SEC["1m"])
    assert [r["ts"] for r in rows] == [T0, T0 + SEC["1m"]]
    assert rows[0]["count"] == 2 and rows[0]["ok"] == 1 and rows[0]["fail"] == 1
    assert rows[1]["count"] == 1 and rows[1]["ok"] == 1
    # 任一维度不同 → 读不到（bucket/task/node/dns/url 全匹配才返回）
    assert s.agg_read("1m", "t1", nb, "", URL, T0, T0)[0]["count"] == 1
    assert s.agg_read("5m", "t1", na, "", URL, T0, T0) == []
    assert s.agg_read("1m", "t2", na, "", URL, T0, T0) == []
    assert s.agg_read("1m", "t1", na, "8.8.8.8", URL, T0, T0) == []
    assert s.agg_read("1m", "t1", na, "", "https://other.com", T0, T0) == []
    # 区间为双端闭：[T0, T0+60) 不含 T0+60 桶；[T0+60, T0+120] 含末桶
    assert [r["ts"] for r in s.agg_read(
        "1m", "t1", na, "", URL, T0, T0 + SEC["1m"] - 1)] == [T0]
    assert [r["ts"] for r in s.agg_read(
        "1m", "t1", na, "", URL, T0 + SEC["1m"], T0 + 2 * SEC["1m"])] == [T0 + SEC["1m"]]
