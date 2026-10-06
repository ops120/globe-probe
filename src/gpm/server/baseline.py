"""动态基线：同小时历史基线 + 稳健 z-score（中位数/MAD）——设计见 .docs/AI_BASELINE_DESIGN.md。

口径与红线：
- 基线从现有 aggregates 1h 桶算（零新存储），按**流级**（task×node×dns×url，与事件机同粒度）判定；
- 诚实性：冷启动/样本不足/历史恒定（无界指标）→ 如实「不评估+原因」，绝不编造基线；
- 有界指标（avail_rate/loss_rate）历史恒定时改用绝对带宽单边判定——恒定可用率突跌
  恰是最需要免拍阈值抓的事件（评审 P0 修正）；
- 基线时间窗四个旋钮由规则 params 控制（rolling/fixed、长度、对齐、排除时段），
  建规则时做「对齐×天数×样本门槛」可行域联动校验（评审 P0：防「永不评估」的组合）。
"""
from __future__ import annotations

import json
import statistics
import time

from ..config import Config

_cfg: Config | None = None

#: 有界指标的绝对带宽（历史恒定时的单边判定带宽）
BOUNDED_BANDS = {"avail_rate": 0.05, "loss_rate": 0.02}
METRIC_FIELDS = ("rtt_avg", "avail_rate", "loss_rate")
#: direction 默认随指标：rtt 变差=变慢(up)、avail 变差=下降(down)、
#: loss 变差=**上升(up)**——丢包突增是上升沿，down 映射只认下降会全聋（复核 P1 修正）
DEFAULT_DIRECTION = {"rtt_avg": "up", "avail_rate": "down", "loss_rate": "up"}
BOUNDED = ("avail_rate", "loss_rate")
DAY = 86400

# 全局默认（cfg.alert.anomaly_* 可覆盖，见 init）
DEFAULT_BASELINE_DAYS = 14
DEFAULT_MIN_SAMPLES = 20
DEFAULT_K = 3.0
DEFAULT_MIN_CONSECUTIVE = 2


def init(cfg) -> None:
    global _cfg, DEFAULT_BASELINE_DAYS, DEFAULT_MIN_SAMPLES, DEFAULT_K, DEFAULT_MIN_CONSECUTIVE
    _cfg = cfg
    a = getattr(cfg, "alert", {}) if cfg else {}
    try:
        DEFAULT_BASELINE_DAYS = max(3, int(a.get("anomaly_baseline_days", 14) or 14))
        DEFAULT_MIN_SAMPLES = max(5, int(a.get("anomaly_min_samples", 20) or 20))
        DEFAULT_K = min(10.0, max(1.5, float(a.get("anomaly_default_k", 3.0) or 3.0)))
        DEFAULT_MIN_CONSECUTIVE = max(1, int(a.get("anomaly_min_consecutive", 2) or 2))
    except Exception:
        pass


def params_of(rule: dict) -> dict:
    """解析规则参数。兼容两种形态：list_rules 返回的 `params`（dict，params_json 已
    转换）与直接查库行的 `params_json`（raw 字符串）——**只认 params_json 会在评估
    主路径上拿到空参数**（list_rules 已 pop 掉它），anomaly 就永远跑默认值。"""
    try:
        p = rule.get("params")
        if p is None:
            p = json.loads(rule.get("params_json") or "{}")
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _with_defaults(p: dict) -> dict:
    """补默认值 + 类型规整（不校验可行域，校验在 validate_params）。

    取值一律用「is None」判缺省而非 `or`——显式 0 是有意义的输入（k=0 钳到下限、
    min_samples=0 钳到 5），用 `or` 会把 0 静默换成全局默认（复核 P2 真值门）。"""
    def _g(key, default):
        v = p.get(key)
        return default if v is None or v == "" else v

    mf = str(_g("metric_field", "avail_rate"))
    out = {
        "metric_field": mf,
        "k": min(10.0, max(1.5, float(_g("k", DEFAULT_K)))),
        "direction": str(_g("direction", DEFAULT_DIRECTION.get(mf) or "both")),
        "min_samples": max(5, int(_g("min_samples", DEFAULT_MIN_SAMPLES))),
        "min_consecutive": max(1, int(_g("min_consecutive", DEFAULT_MIN_CONSECUTIVE))),
        "window_mode": str(_g("window_mode", "rolling")),
        "baseline_days": max(3, int(_g("baseline_days", DEFAULT_BASELINE_DAYS))),
        "baseline_from": str(_g("baseline_from", "")),
        "align": str(_g("align", "hour")),
        "exclude_windows": _g("exclude_windows", []) or [],
    }
    if out["direction"] not in ("both", "up", "down"):
        out["direction"] = "both"
    if out["align"] not in ("hour", "weekday_hour"):
        out["align"] = "hour"
    if out["window_mode"] not in ("rolling", "fixed"):
        out["window_mode"] = "rolling"
    if out["metric_field"] not in METRIC_FIELDS:
        out["metric_field"] = "avail_rate"
    return out


def validate_params(p: dict) -> list[str]:
    """建/改规则时的完整校验，返回中文错误列表（空=通过）。"""
    errs: list[str] = []
    try:
        q = _with_defaults(p or {})
    except (TypeError, ValueError) as e:
        return [f"参数类型非法: {e}"]
    # 用**原始入参**校验：_with_defaults 已把非法值静默改成 avail_rate，
    # 校验规整后的值等于死代码——「bogus」会原样入库并静默按可用率评估（复核 P2）
    _raw_mf = (p or {}).get("metric_field")
    if _raw_mf is not None and _raw_mf not in METRIC_FIELDS:
        errs.append(f"metric_field 只支持 {'/'.join(METRIC_FIELDS)}，收到 {_raw_mf!r}")
    if q["window_mode"] == "fixed" and not q["baseline_from"]:
        errs.append("fixed 窗口必须提供 baseline_from（YYYY-MM-DD）")
    for d in q["exclude_windows"]:
        if not _parse_range(d):
            errs.append(f"排除时段格式非法：{d!r}（应为 YYYY-MM-DD~YYYY-MM-DD）")
    if len(q["exclude_windows"]) > 10:
        errs.append("排除时段最多 10 段")
    # 可行域联动（评审 P0）：对齐×天数×样本门槛——防止「永不评估」的组合
    per_day = 3 if q["align"] == "hour" else 1
    need_days = -(-q["min_samples"] // per_day) * (7 if q["align"] == "weekday_hour" else 1)
    if q["baseline_days"] < need_days:
        fix = (f"把 baseline_days 提到 ≥{need_days}，或把 min_samples 降到 "
               f"≤{q['baseline_days'] * per_day // (7 if q['align'] == 'weekday_hour' else 1)}")
        errs.append(f"基线窗口不可行：{q['align']} 对齐 + {q['baseline_days']} 天 + "
                    f"min_samples={q['min_samples']} 永远凑不满样本（{q['align']} 对齐每天最多 "
                    f"{per_day} 个样本）。{fix}")
    return errs


def _parse_date(s: str):
    """YYYY-MM-DD → 当日本地 00:00 的 epoch 秒；非法返回 None。"""
    try:
        st = time.strptime(str(s).strip(), "%Y-%m-%d")
        return int(time.mktime(st))
    except (ValueError, TypeError):
        return None


def _parse_range(s: str):
    """'YYYY-MM-DD~YYYY-MM-DD' → (from_epoch, to_epoch)；非法返回 None。"""
    parts = str(s).split("~")
    if len(parts) != 2:
        return None
    a, b = _parse_date(parts[0]), _parse_date(parts[1])
    if a is None or b is None or b < a:
        return None
    return a, b + DAY - 1


def _aligned_timestamps(cur_ts: int, win_from: int, win_to: int, align: str) -> list:
    """基线候选桶时间戳：窗口内与当前桶同「对齐位」的历史时间戳。

    hour：同小时 ±1（日内周期）；weekday_hour：同星期几且同小时（周周期，
    每天最多 1 个样本——样本预算见可行域校验）。按窗口边界生成（fixed 窗口
    可以比 rolling 长），窗口外一律不进。"""
    wd = time.localtime(cur_ts).tm_wday
    out = []
    d = 0
    while d * DAY <= (cur_ts - win_from) + DAY:      # 保险丝：窗口上界之外再多看一天
        base = cur_ts - d * DAY
        if base < win_from - DAY:
            break
        for off in (-1, 0, 1):
            ts = base + off * 3600
            if ts < win_from or ts > win_to:
                continue
            if align == "weekday_hour" and time.localtime(ts).tm_wday != wd:
                continue
            out.append(ts)
        d += 1
    return sorted(set(out))


def baseline(storage, task_id: str, p: dict, ts_now: int) -> dict:
    """计算任务级动态基线（按流判定），返回可直接渲染/评估的结果。

    返回 {evaluable, reason?, k, mc, window_desc, streams: [...]}；
    每个 stream：{key, node_id, dns, url, evaluable, reason?, fired, worst_z, cur_v,
                  center, scale, samples, mode, zs: [(ts, v, z)]}。
    """
    q = _with_defaults(p or {})
    mf, k = q["metric_field"], q["k"]
    mc = q["min_consecutive"]
    cur_ts = ts_now // 3600 * 3600 - 3600          # 最后一个已完结 1h 桶
    eval_ts = [cur_ts - i * 3600 for i in range(mc)]   # 评估桶=最近 mc 个已完结桶（降序）

    # 基线窗口（rolling/fixed）与候选对齐位
    if q["window_mode"] == "fixed":
        start = _parse_date(q["baseline_from"])
        # 终点=min(起点+长度, 首个评估桶前一夜)——窗口自动向「现在」封口，
        # 被评估桶永不进基线；起点+长度伸到未来的部分自然截断
        win_from, win_to = start, min(start + q["baseline_days"] * DAY - 1, eval_ts[-1] - 1)
    else:
        win_from, win_to = cur_ts - q["baseline_days"] * DAY, cur_ts - mc * 3600 - 1
    excl = [r for r in (_parse_range(x) for x in q["exclude_windows"]) if r]

    # 候选对齐位（不含评估桶本身，避免基线被被评估值污染）
    cand = [t for t in _aligned_timestamps(eval_ts[0], win_from, win_to, q["align"])
            if t < eval_ts[-1]]

    # 排除：手动排除时段 + 维护窗口（逐桶查库，42 桶以内代价可忽略）
    cand = [t for t in cand if not any(a <= t <= b for a, b in excl)]
    kept: list[int] = []
    try:
        for ts_b in cand:
            if not storage.in_maintenance(ts_b, task_id=task_id):
                kept.append(ts_b)
    except Exception:  # noqa: BLE001 - 维护窗口查询失败按「无维护窗口」处理
        kept = list(cand)
    cand = kept

    # 取值：窗口内该任务全部流一次查回，按流分组
    rows = storage.agg_metric_values(task_id, "1h", mf, win_from, max(win_to, cur_ts))
    by_stream: dict = {}
    for r in rows:
        if int(r["ts"]) in cand:
            by_stream.setdefault((r["node_id"], r["dns"], r["url"]), {})[int(r["ts"])] = \
                (None if r["value"] is None else float(r["value"]))
    cur_rows = [r for r in rows if int(r["ts"]) in set(eval_ts)]
    cur_by_stream: dict = {}
    for r in cur_rows:
        cur_by_stream.setdefault((r["node_id"], r["dns"], r["url"]), {})[int(r["ts"])] = \
            (None if r["value"] is None else float(r["value"]))

    band = BOUNDED_BANDS.get(mf)
    direction = q["direction"]
    streams = []
    for key, vals in sorted(by_stream.items()):
        node_id, dns, url = key
        st = {"key": "%s|%s|%s|%s" % (task_id, node_id, dns or "", url or ""),
              "node_id": node_id, "dns": dns, "url": url,
              "evaluable": False, "fired": False, "worst_z": None, "cur_v": None,
              "center": None, "scale": None, "samples": 0, "mode": "zscore", "zs": []}
        streams.append(st)
        B = [v for t, v in sorted(vals.items()) if v is not None]
        if len(B) < q["min_samples"]:
            st["reason"] = f"基线样本不足（n={len(B)} < {q['min_samples']}）"
            continue
        center = statistics.median(B)
        mad = statistics.median(abs(x - center) for x in B)
        scale = 1.4826 * mad
        mode = "zscore"
        if scale < 1e-9:
            scale = statistics.pstdev(B)
        if scale < 1e-9:
            if not band:
                st["reason"] = "历史恒定无波动（无界指标无从定义偏离）"
                continue
            mode = "bounded"                     # 有界指标恒定 → 绝对带宽单边判定
            scale = band
        st.update({"evaluable": True, "center": round(center, 4),
                   "scale": round(scale, 4), "samples": len(B), "mode": mode})
        # 有界模式的有效门槛=1.0：带宽本身就是「越界即偏离」的定义（z=dev/band），
        # 若再乘 k 等于把门槛抬高 k 倍（0.06/0.05=1.2<3 → 永不触发，评审同类问题）
        eff_k = 1.0 if mode == "bounded" else k
        st["eff_k"] = eff_k
        cvs = cur_by_stream.get(key, {})
        # 连续 mc 桶逐桶偏离
        dev_ok = True
        worst = None
        for t in sorted(eval_ts):
            v = cvs.get(t)
            if v is None:
                dev_ok = False
                st["zs"].append((t, None, None))
                continue
            if mode == "bounded":
                # down：v 低于 center-band 为正偏离；up：v 高于 center+band；both 取幅值
                if direction == "down":
                    z = (center - v) / scale
                elif direction == "up":
                    z = (v - center) / scale
                else:
                    z = max(center - v, v - center) / scale
            else:
                z = (v - center) / scale
                if direction == "down":
                    z = -z
                elif direction == "both":
                    z = abs(z)
            st["zs"].append((t, v, round(z, 2)))
            if worst is None or abs(z) > abs(worst):
                worst = z
            if z < eff_k:
                dev_ok = False
        st["worst_z"] = round(worst, 2) if worst is not None else None
        st["cur_v"] = cvs.get(eval_ts[0])
        st["fired"] = bool(dev_ok and worst is not None and abs(worst) >= eff_k)
    # eff_k：恢复滞回阈值口径基准——zscore=k / bounded=1.0（流级 st["eff_k"]）。
    # 恢复判定必须用 0.8*eff_k，否则 bounded 模式 z∈[1.0, 0.8k) 会 firing↔resolved 振铃
    return {"evaluable": any(s["evaluable"] for s in streams), "k": k, "mc": mc,
            "metric_field": mf, "direction": direction,
            "window_desc": "%s %d 天 · %s 对齐 · 排除 %d 段" % (
                q["window_mode"], q["baseline_days"], q["align"], len(q["exclude_windows"])),
            "streams": streams}


def preview(storage, task_id: str, p: dict, ts_now: int) -> dict:
    """给 /api/baseline 用：基线统计 + 最近桶 z 序列（不评估 firing）。"""
    r = baseline(storage, task_id, p, ts_now)
    return r
