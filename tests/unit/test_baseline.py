"""baseline.py 单元测试：动态基线计算（第十期）。

钉住设计文档的关键行为：
- 中位数/MAD 稳健 z-score；同小时±1 对齐只取基线窗口内对齐位
- 有界指标（avail/loss）历史恒定时绝对带宽判定（评审 P0：不拒评）
- 无界指标历史恒定 → 如实拒评；冷启动样本不足 → 如实拒评
- 可行域联动校验（weekday_hour × 14 天 × min_samples=20 死锁挡在入口）
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpm.server import baseline  # noqa: E402

DAY = 86400
NOW = 1_800_000_000
CUR = NOW // 3600 * 3600 - 3600        # 最后一个已完结 1h 桶


class FakeStorage:
    """baseline 只需要 agg_metric_values + in_maintenance。"""

    def __init__(self, buckets=None, maintenance=False):
        self.buckets = buckets or {}       # (ts, node, dns, url) -> value
        self.maintenance = maintenance

    def agg_metric_values(self, task_id, bucket, metric_field, t_from, t_to):
        assert metric_field in ("rtt_avg", "avail_rate", "loss_rate")
        return [{"ts": ts, "node_id": n, "dns": d, "url": u, "value": v}
                for (ts, n, d, u), v in self.buckets.items()
                if t_from <= ts <= t_to and v is not None]

    def in_maintenance(self, ts, task_id="", node_id=""):
        return self.maintenance


def seed_history(storage, base_value, days=14, node="n1", jitter=1.0, cur_value=None,
                 cur_prev_value=None):
    """种历史：每天同小时±1 共 3 桶；当前评估桶与前一桶可选。"""
    for d in range(1, days + 1):
        for off in (-1, 0, 1):
            ts = CUR - d * DAY + off * 3600
            storage.buckets[(ts, node, "", "")] = base_value + (jitter if d % 2 else -jitter)
    if cur_prev_value is not None:
        storage.buckets[(CUR - 3600, node, "", "")] = cur_prev_value
    if cur_value is not None:
        storage.buckets[(CUR, node, "", "")] = cur_value


P = {"metric_field": "rtt_avg", "min_samples": 20, "baseline_days": 14,
     "min_consecutive": 2, "k": 3.0, "align": "hour", "window_mode": "rolling"}


def test_zscore_fires_on_step_change():
    """历史 10±1ms，当前两桶 50ms → firing，z 远超 3。"""
    st = FakeStorage()
    seed_history(st, 10.0, cur_value=50.0, cur_prev_value=50.0)
    r = baseline.baseline(st, "t1", P, NOW)
    assert r["evaluable"]
    s = r["streams"][0]
    assert s["evaluable"] and s["fired"] and s["worst_z"] > 10, s
    assert 9 <= s["center"] <= 11


def test_bounded_avail_constant_history_drops():
    """评审 P0 回归钉：avail 历史≡1.0（MAD=0），当前跌到 0.94 → 触发而非拒评。"""
    st = FakeStorage()
    seed_history(st, 1.0, jitter=0, cur_value=0.94, cur_prev_value=0.94)
    r = baseline.baseline(st, "t1", {**P, "metric_field": "avail_rate"}, NOW)
    s = r["streams"][0]
    assert s["evaluable"] and s["mode"] == "bounded"
    assert s["fired"], s


def test_bounded_avail_up_does_not_fire_by_default():
    """avail 默认 direction=down：可用率回到 1.0（变好）不触发。"""
    st = FakeStorage()
    seed_history(st, 0.97, jitter=0, cur_value=1.0, cur_prev_value=1.0)
    r = baseline.baseline(st, "t1", {**P, "metric_field": "avail_rate"}, NOW)
    assert not r["streams"][0]["fired"]


def test_rtt_constant_history_honestly_rejected():
    """无界指标历史恒定 → 如实拒评（不编基线），理由可读。"""
    st = FakeStorage()
    seed_history(st, 10.0, jitter=0, cur_value=10.0, cur_prev_value=10.0)
    r = baseline.baseline(st, "t1", P, NOW)
    s = r["streams"][0]
    assert not s["evaluable"] and "恒定" in (s.get("reason") or "")


def test_cold_start_short_history():
    """冷启动：样本不足 → 拒评并给原因（不编基线）。"""
    st = FakeStorage()
    seed_history(st, 10.0, days=3, cur_value=50.0, cur_prev_value=50.0)
    r = baseline.baseline(st, "t1", P, NOW)
    assert not r["evaluable"]
    assert "样本不足" in (r["streams"][0].get("reason") or "")


def test_hour_alignment_ignores_other_hours():
    """同小时±1 对齐：每天 20 个异小时诱饵（50ms）不进基线。

    判别靠样本数：对齐位恰 40 个（14 天×3 桶−2 个被评估桶）；对齐失效的话诱饵
    会让样本数飙到 300+。"""
    st = FakeStorage()
    seed_history(st, 10.0, days=14)
    import datetime as _dt
    cur_hour = _dt.datetime.fromtimestamp(CUR).hour
    aligned_hours = {(cur_hour + off) % 24 for off in (-1, 0, 1)}   # 同小时±1 对齐位
    day0 = _dt.datetime.fromtimestamp(CUR).replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(1, 15):
        for h in range(24):
            if h in aligned_hours:
                continue
            # 按「本地零点 + h」种桶：CUR 本身锚在某个小时上，直接加 h*3600 会落到别的小时
            ts = int((day0 - _dt.timedelta(days=d)).timestamp()) + h * 3600
            st.buckets[(ts, "n1", "", "")] = 50.0
    r = baseline.baseline(st, "t1", P, NOW)
    s = r["streams"][0]
    # 41 = 14 天×3 对齐位 −1 个窗口起点外的对齐位；诱饵（50）若混入，中位数会被显著拉高
    assert s["evaluable"] and s["samples"] == 41, s.get("samples")
    assert 9 <= s["center"] <= 12, "异小时诱饵不得进基线"
    assert not s["fired"]


def test_min_consecutive_requires_two_buckets():
    """当前桶偏离、前一桶正常 → 不触发（连续性门槛）。"""
    st = FakeStorage()
    seed_history(st, 10.0, cur_value=50.0)       # 只种当前偏离，前一桶不种（缺失也算不连续）
    r = baseline.baseline(st, "t1", P, NOW)
    assert not r["streams"][0]["fired"]


def test_exclude_windows_removes_buckets():
    """手动排除时段：区间内的历史桶不进基线。

    判别用差值法：与「无排除」对照，被排除日的 ±1h 三桶恰好退出样本集——
    （刻意不用常数历史：无界指标在常数历史下会诚实拒评，那是另一个测试钉的场景。）"""
    import datetime
    st = FakeStorage()
    seed_history(st, 10.0, days=14)
    st.buckets[(CUR - DAY, "n1", "", "")] = 40.0     # 被排除日的桶抬高成 40
    d = datetime.datetime.fromtimestamp(CUR - DAY)
    dstr = d.strftime("%Y-%m-%d")
    r0 = baseline.baseline(st, "t1", P, NOW)         # 无排除对照
    p = {**P, "exclude_windows": [f"{dstr}~{dstr}"]}
    r = baseline.baseline(st, "t1", p, NOW)
    s = r["streams"][0]
    assert s["evaluable"] and r0["streams"][0]["evaluable"]
    assert s["samples"] == r0["streams"][0]["samples"] - 3,         (s.get("samples"), r0["streams"][0]["samples"])
    assert 9 <= s["center"] <= 11, "排除日内的桶（40）不得进基线"


def test_fixed_window_uses_anchor():
    """fixed 模式：只取 [baseline_from, 起点+天数]（自动向现在封口）；窗外桶不进基线。"""
    import datetime
    st = FakeStorage()
    seed_history(st, 10.0, days=14)
    # 起点锚在 20 天前 → 窗口=[-20d, -6d]：昨天（-1d，值 60）在窗外
    st.buckets[(CUR - DAY, "n1", "", "")] = 60.0
    d20 = datetime.datetime.fromtimestamp(CUR - 20 * DAY).strftime("%Y-%m-%d")
    r = baseline.baseline(st, "t1", {**P, "window_mode": "fixed", "baseline_from": d20,
                                     "min_samples": 20}, NOW)
    s = r["streams"][0]
    assert s["evaluable"] and s["samples"] == 24, s.get("samples")
    # 24 = 窗口 [起点 15:00, +14d] 与有种子数据日期的交集（起点按本地午夜锚定）；
    # rolling 同参数会看到 41 个样本——样本数本身证明了窗口被起点锚定
    assert 9 <= s["center"] <= 11, "fixed 窗外桶（60）不得进基线"


def test_validate_params_feasibility():
    """可行域联动校验：weekday_hour 死锁组合挡在入口。"""
    ok = baseline.validate_params({"metric_field": "rtt_avg", "min_samples": 20,
                                   "baseline_days": 14, "align": "hour"})
    assert ok == []
    errs = baseline.validate_params({"metric_field": "rtt_avg", "min_samples": 20,
                                     "baseline_days": 14, "align": "weekday_hour"})
    assert errs and "不可行" in errs[0]
    errs = baseline.validate_params({"metric_field": "rtt_avg", "window_mode": "fixed"})
    assert any("baseline_from" in e for e in errs)


def test_maintenance_buckets_excluded():
    """维护窗口内的历史桶不进基线（豁免语义与告警一致）。"""
    st = FakeStorage(maintenance=True)
    seed_history(st, 10.0, cur_value=50.0, cur_prev_value=50.0)
    r = baseline.baseline(st, "t1", P, NOW)
    assert not r["evaluable"], "全部桶都在维护窗口内 → 样本不足拒评"
