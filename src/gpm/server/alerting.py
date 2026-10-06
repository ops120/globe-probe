"""告警规则评估与派发 —— P0 告警闭环的核心。

设计要点（与项目红线一致）：
- 只读聚合表（1m / 1h），**不扫原始结果表**
- 命中维护窗口 → 本轮跳过评估，也不告警
- 同一「规则 + 目标」在 firing 期间不重复打扰：静默期（silence_seconds）内只发一次，
  到期后按需再提醒；条件消失 → 发「已恢复」并把历史里的 firing 标记关闭
- 派发失败只记录到渠道 last_error / 告警 detail，绝不影响主流程

告警即诊断（ONCALL_OPTIMIZATION.md 第一期）：
- firing / remind 通知在原有行之后追加固定段落：【范围】（各节点最近一轮 → diagnose.verdict）、
  【初判】（最近一次失败 error_class → diagnose.classify）、【持续】（本轮未恢复时长）、
  【证据】（该目标最近一次失败的关键证据）、【链接】（public_url 深链）；
  数据缺失的段落整体省略，恢复（resolved）通知保持原有格式（向后兼容）。
- 升级链：规则配 escalate_minutes（0=关，≤1440）后，firing 超过该时长仍未确认 →
  以【升级】前缀重新通知同一渠道，两次升级间隔不小于 escalate_minutes；
  上次升级时间持久化在升级行 alerts.detail（"escalated_at=<ts>"），重启不丢；
  仍存在未确认的关联事件才继续升级，事件全部确认或已随恢复关闭即停止；恢复后不再升级。
"""
from __future__ import annotations

import logging
import threading

from ..config import Config
from . import diagnose as _diagnose

try:  # 通知模块由独立模块提供；缺失时降级为「仅记录、不发送」
    from . import notify as _notify
except Exception:  # noqa: BLE001
    _notify = None  # type: ignore[assignment]

log = logging.getLogger("gpm.alerts")

# 指标元信息：中文名 / 默认比较方向 / 单位
METRICS = {
    "avail": ("可用率", "lt", "%", True),
    "rtt_avg": ("延迟均值", "gt", "ms", True),
    "rtt_p95": ("延迟 P95", "gt", "ms", True),
    "loss": ("丢包率", "gt", "%", True),
    "node_offline": ("节点离线", "eq", "", False),
    "anomaly": ("动态基线偏离", "gt", "kσ", True),   # 第十期：与自己的历史同时段比，无需手拍阈值
}
OPS = {"lt": "<", "gt": ">", "eq": "=", "ne": "!="}

# 范围判定的「最近一轮」窗口（秒）：契约要求 ≤60s，storage 侧同样钳制
# —— 由 cfg.alert.scope_window_seconds 提供；保留模块级同名常量供旧调用按属性名读取。
SCOPE_WINDOW_SECONDS = 60
# 升级链阈值上限（分钟）—— 由 cfg.alert.escalate_max_minutes 提供。
ESCALATE_MAX_MINUTES = 1440

_cfg: Config | None = None


def init(cfg) -> None:
    """由 app 在启动时注入 cfg；同步 SCOPE_WINDOW_SECONDS / ESCALATE_MAX_MINUTES /
    RETRY_BACKOFF 到 cfg.alert.*。"""
    global _cfg, SCOPE_WINDOW_SECONDS, ESCALATE_MAX_MINUTES, RETRY_BACKOFF
    _cfg = cfg
    try:
        SCOPE_WINDOW_SECONDS = int(cfg.alert.get("scope_window_seconds", SCOPE_WINDOW_SECONDS) or SCOPE_WINDOW_SECONDS)
    except Exception:
        pass
    try:
        ESCALATE_MAX_MINUTES = int(cfg.alert.get("escalate_max_minutes", ESCALATE_MAX_MINUTES) or ESCALATE_MAX_MINUTES)
    except Exception:
        pass
    try:
        v = cfg.alert.get("retry_backoff_seconds", RETRY_BACKOFF)
        if isinstance(v, (list, tuple)) and all(isinstance(x, (int, float)) for x in v):
            RETRY_BACKOFF = tuple(int(x) for x in v)
    except Exception:
        pass



def _r1(v):
    return None if v is None else round(v, 2)


def window_stats(storage, task_id: str, window_seconds: int, ts: int) -> dict:
    """窗口内聚合（1m / 1h 桶）：可用率、RTT 均值/P95、丢包率。"""
    win = max(60, int(window_seconds or 300))
    bucket = "1m" if win <= 3600 else "1h"
    step = 60 if bucket == "1m" else 3600
    t_to = ts // step * step
    t_from = t_to - win
    rows = storage.agg_buckets_existing(bucket, task_id, t_from, t_to)
    count = sum(int(r["count"] or 0) for r in rows)
    ok = sum(int(r["ok"] or 0) for r in rows)
    # 均值按样本数加权；P95 取窗口内最大值（跨桶 P95 属近似，与项目既有约定一致）
    wr = [(float(r["rtt_avg"]), int(r["count"] or 0)) for r in rows if r["rtt_avg"] is not None]
    wsum = sum(w for _, w in wr) or 0
    p95s = [float(r["rtt_p95"]) for r in rows if r.get("rtt_p95") is not None]
    losses = [(float(r["loss_rate"]) * int(r["count"] or 0), int(r["count"] or 0))
              for r in rows if r.get("loss_rate") is not None]
    lsum = sum(w for _, w in losses) or 0
    return {
        "count": count, "ok": ok, "fail": count - ok,
        "avail": round(ok / count, 4) if count else None,
        "rtt_avg": round(sum(v * w for v, w in wr) / wsum, 2) if wsum else None,
        "rtt_p95": round(max(p95s), 2) if p95s else None,
        "loss": round(sum(v for v, _ in losses) / lsum, 4) if lsum else None,
        "bucket": bucket, "window_seconds": win,
    }


def _compare(value, op: str, threshold: float) -> bool:
    if value is None:
        return False
    if op == "lt":
        return value < threshold
    if op == "gt":
        return value > threshold
    if op == "eq":
        return abs(value - threshold) < 1e-9
    if op == "ne":
        return abs(value - threshold) >= 1e-9
    return False


def _scaled(metric: str, value):
    """可用率/丢包率按百分比展示（0.725 → 72.5）。"""
    if value is None:
        return None
    if metric in ("avail", "loss"):
        return round(value * 100, 2)
    return round(value, 2)


def _fmt(rule: dict, label: str, value, stats: dict, ts: int, kind: str) -> tuple[str, str]:
    from ..common.util import now as _now  # noqa: F401  (保持与全局时间一致)
    metric = rule["metric"]
    name, _d, unit, _agg = METRICS.get(metric, (metric, "gt", "", True))
    shown = _scaled(metric, value)
    when = _time_text(ts)
    if metric == "node_offline":
        head = "节点 " + label + (" 离线" if kind == "firing" else " 已恢复")
        title = ("【告警】" if kind == "firing" else "【恢复】") + head
        body = "\n".join([
            "- 规则：" + rule["name"] + "（" + name + "）",
            "- 节点：" + label,
            "- 时间：" + when,
            "- 说明：节点心跳超时（60s 内无心跳）",
        ])
        return title, body
    if kind == "resolved":
        title = "【恢复】" + label + " " + name + " 已恢复"
    elif kind == "remind":
        title = "【提醒】" + label + " " + name + " 仍未恢复"
    else:
        title = "【告警】" + label + " " + name + " " + str(shown) + unit + " " + OPS.get(rule["op"], "") \
            + " " + str(_scaled(metric, rule["threshold"])) + unit
    body = "\n".join([
        "- 规则：" + rule["name"] + "（" + name + " " + OPS.get(rule["op"], "") + " "
        + str(_scaled(metric, rule["threshold"])) + unit + "，窗口 " + str(stats.get("window_seconds")) + "s）",
        "- 目标：" + label,
        "- 当前值：" + (str(shown) + unit if shown is not None else "无数据"),
        "- 样本：" + str(stats.get("count")) + " 条（成功 " + str(stats.get("ok"))
        + " / 失败 " + str(stats.get("fail")) + "）",
        "- 时间：" + when,
    ])
    return title, body


def _time_text(ts: int) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ---------------------------------------------------------------- 告警即诊断

def _safe_call(storage, name: str, *args, default=None, **kw):
    """只读调用的容错边界：storage 方法缺失或抛错时返回 default（与 eventview 同约定）。

    诊断段落缺失绝不能影响告警主流程，但也不能完全无痕——否则发出的告警看似完整，
    【证据】【范围】却悄悄消失，值班的人无从知道那块为什么空了。"""
    fn = getattr(storage, name, None)
    if not callable(fn):
        return default
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001 - 诊断段落缺失绝不影响告警主流程
        log.warning("告警诊断数据 %s 读取失败（该段落将以空态出现在告警文本里）: %s", name, e)
        return default


def _escalate_minutes(rule: dict) -> int:
    try:
        v = int((rule or {}).get("escalate_minutes") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(v, ESCALATE_MAX_MINUTES))


def _scope_line(storage, task_id: str, ts: int) -> str:
    """【范围】段落：各节点最近一轮探测状态 → diagnose.verdict 三档结论。

    契约：verdict.verdict + "（f/n 节点）"；partial/single_node 追加失败节点名
    （最多 4 个）便于直接定位。无失败样本（mode 为空）时整段省略。
    """
    if not task_id:
        return ""
    states = _safe_call(storage, "task_nodes_latest_status", task_id, ts,
                        SCOPE_WINDOW_SECONDS, default=[]) or []
    v = _diagnose.verdict([{"node_name": str(s.get("node_name") or s.get("node_id") or ""),
                            "status": s.get("status")} for s in states])
    if not v["mode"]:
        return ""
    line = "【范围】" + v["verdict"] + "（" + str(v["failed"]) + "/" + str(v["total"]) + " 节点）"
    if v["mode"] in ("partial", "single_node") and v["failed_names"]:
        line += "：" + "、".join(str(x) for x in v["failed_names"][:4])
    return line


def _stage_text(m: dict | None) -> str:
    """关键证据的阶段耗时摘要：curl 给阶段耗时，mtr 给末跳丢包。"""
    segs = []
    for key, label in (("dns_time", "DNS"), ("connect_time", "连接"), ("tls_time", "TLS"),
                       ("ttfb", "首字节"), ("total_time", "总耗时")):
        v = (m or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            segs.append(label + " " + str(round(float(v))) + "ms")
    if not segs:
        hops = (m or {}).get("hops")
        if isinstance(hops, list) and hops and isinstance(hops[-1], dict):
            last = hops[-1]
            segs.append("mtr 末跳 " + str(last.get("host") or "?")
                        + " 丢包 " + str(last.get("loss_pct") or 0) + "%")
    return " / ".join(segs)


def _evidence_line(storage, task_id: str, ts: int, node_names: dict, ev: dict | None) -> str:
    """【证据】段落：该目标最近一次失败探测的时间 / 节点 / 错误 / 阶段耗时。"""
    if not ev:
        return ""
    nid = str(ev.get("node_id") or "")
    parts = [_time_text(int(ev.get("ts") or ts))]
    who = node_names.get(nid, nid)
    if who:
        parts.append("节点 " + str(who))
    ip = str(ev.get("resolved_ip") or "").strip()
    if ip:
        parts.append("解析 " + ip)
    head = str(ev.get("error") or "").strip() or str(ev.get("error_class") or "").strip() or "失败"
    line = "【证据】" + " · ".join(parts) + " · " + head
    stage = _stage_text(ev.get("metrics") if isinstance(ev.get("metrics"), dict) else {})
    if stage:
        line += "（" + stage + "）"
    return line


def _init_line(ev: dict | None) -> str:
    """【初判】段落：最近一次失败的 error_class → diagnose.classify（层面 — 建议）。"""
    if not ev:
        return ""
    layer, advice = _diagnose.classify(str(ev.get("error_class") or ""))
    return "【初判】" + layer + " — " + advice


def _duration_text(t0: int, stats: dict, ts: int) -> str:
    """【持续】段落：本轮未恢复起点至今的分钟数 + 窗口内失败样本数。"""
    if t0 <= 0 or ts < t0:
        return ""
    fails = stats.get("fail") if isinstance(stats, dict) else None
    tail = ""
    try:
        if fails is not None:
            tail = "（" + str(max(0, int(fails))) + " 次失败）"
    except (TypeError, ValueError):
        tail = ""
    seconds = int(ts - t0)
    if seconds < 60:
        return "【持续】已持续不足 1 分钟" + tail
    return "【持续】已持续 " + str(seconds // 60) + " 分钟" + tail


def public_url(storage) -> str:
    """深链前缀。取配置 `server.public_url` 优先，其次 settings 里的同名键。

    历史缺陷：只有 setting_get 一条来源，而 **没有任何地方调用 setting_set("public_url")**
    —— 没有 config 键也没有接口，等于线上根本配不了，于是每条通知都没有【链接】段落
    （.docs/ONCALL_OPTIMIZATION_2.md 第三期 12）。
    """
    value = str(_safe_call(storage, "setting_get", "public_url", "", default="") or "")
    return value.strip().rstrip("/")


def _link_line(storage, task_id: str, ts_start: int) -> str:
    """【链接】段落：public_url 未配置时整段省略（不编造链接）。"""
    if not task_id:
        return ""
    base = public_url(storage)
    if not base:
        return ""
    return ("【链接】" + base.rstrip("/") + "/index.html?task=" + str(task_id)
            + "&ts=" + str(int(ts_start or 0)))


def _incident_started_at(storage, metric: str, key: str) -> int:
    """首个关联未恢复事件的开始时间（首轮 firing 时 alerts 行还没落库的兜底）。

    metric=node_offline 按 node_id 关联，其余按 task_id 关联；没有则返回 0。
    """
    incidents = _safe_call(storage, "list_incidents", default=[], limit=100, open_only=True)
    if not isinstance(incidents, list):
        return 0
    field = "node_id" if metric == "node_offline" else "task_id"
    starts = [int(i.get("started_at") or 0) for i in incidents
              if str(i.get(field) or "") == str(key)]
    return min([x for x in starts if x > 0], default=0)


def _diagnosis_lines(storage, rule: dict, key: str, label: str, stats: dict, ts: int,
                     node_names: dict) -> list[str]:
    """通知的固定诊断段落（firing / remind / escalate 共用；resolved 不调用）。

    顺序固定：【范围】【初判】【持续】【证据】【链接】；node_offline 告警没有
    目标任务，范围/初判/证据/链接天然缺数据而省略，只保留【持续】。
    持续时长的起点：本轮告警 episode 起点（最近一次 resolved 后首条 firing），
    首轮 firing 时 alerts 行未落库 → 退回关联未恢复事件的 started_at。
    """
    t_id = "" if rule.get("metric") == "node_offline" else str(key)
    t0 = int(_safe_call(storage, "alert_episode_start", rule["id"], key, default=0) or 0)
    if t0 <= 0:
        t0 = _incident_started_at(storage, str(rule.get("metric") or ""), key)
    ev = None
    if t_id:
        ev = _safe_call(storage, "task_last_failure", t_id, ts, default=None)
    lines: list[str] = []
    for line in (_scope_line(storage, t_id, ts),
                 _init_line(ev),
                 _duration_text(t0, stats, ts),
                 _evidence_line(storage, t_id, ts, node_names, ev),
                 _link_line(storage, t_id, t0 or ts)):
        if line:
            lines.append(line)
    return lines


def flatten(channel: dict) -> dict:
    """存储层是 {type, config:{...}}，通知模块要求扁平字段 → 在这里桥接。"""
    return {"type": channel.get("type"), **(channel.get("config") or {})}


# 失败重投退避（秒）：第 1/2/3 次重试分别等这么久
# —— 由 cfg.alert.retry_backoff_seconds 提供；保留模块级同名常量供旧调用按属性名读取。
RETRY_BACKOFF: tuple[int, ...] = (60, 300, 900)


def _dispatch(storage, channels: list[dict], title: str, text: str, ts: int):
    """派发到全部渠道，返回 (是否有渠道成功, 渠道数, 成功数, 错误摘要, 失败渠道列表)。"""
    if not channels:
        return False, 0, 0, "未配置可用通知渠道", []
    if _notify is None:
        return False, len(channels), 0, "通知模块（notify.py）不可用", list(channels)
    n_ok, errs, failed = 0, [], []
    for ch in channels:
        try:
            ok, msg = _notify.send(flatten(ch), title, text)
        except Exception as e:  # noqa: BLE001 - 通知失败绝不影响评估
            ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
        storage.channel_touch(ch["id"], ok, "" if ok else msg, ts)
        n_ok += 1 if ok else 0
        if not ok:
            errs.append(ch["name"] + ": " + msg)
            failed.append((ch, msg))
    return n_ok > 0, len(channels), n_ok, "; ".join(errs), failed


def retry_pending(storage, ts: int = 0, limit: int = 10) -> list[dict]:
    """重投队列：把派发失败的通知按退避重试（默认 60s / 300s / 900s，3 次后标记 failed）。"""
    import time
    ts = ts or int(time.time())
    out: list[dict] = []
    if _notify is None:
        return out
    chans = {c["id"]: c for c in storage.list_channels()}
    for row in storage.outbox_due(ts, limit):
        ch = chans.get(row["channel_id"])
        if not ch or not ch["enabled"]:
            storage.outbox_mark(row["id"], "failed", "渠道不存在或已停用")
            out.append({"id": row["id"], "status": "failed", "error": "渠道不存在或已停用"})
            continue
        try:
            ok, msg = _notify.send(flatten(ch), row["title"], row["text"])
        except Exception as e:  # noqa: BLE001
            ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
        attempts = int(row["attempts"] or 0)
        if ok:
            storage.outbox_mark(row["id"], "done")
            storage.channel_touch(ch["id"], True, "", ts)
        elif attempts + 1 >= len(RETRY_BACKOFF):
            storage.outbox_mark(row["id"], "failed", msg)
        else:
            storage.outbox_mark(row["id"], "pending", msg, ts + RETRY_BACKOFF[attempts])
        out.append({"id": row["id"], "status": "done" if ok else ("pending" if attempts + 1 < len(RETRY_BACKOFF) else "failed"),
                    "error": "" if ok else msg})
    return out


def retry_one(storage, oid: int, ts: int = 0) -> tuple[bool, str]:
    """手动强制重投一条（UI「立即重投」按钮），不等退避时间。"""
    import time
    ts = ts or int(time.time())
    row = storage.outbox_get(oid)
    if not row:
        return False, "记录不存在"
    if row["status"] == "done":
        return True, "该通知已成功送达"
    ch = next((c for c in storage.list_channels() if c["id"] == row["channel_id"]), None)
    if not ch:
        storage.outbox_mark(oid, "failed", "渠道不存在")
        return False, "渠道不存在"
    if _notify is None:
        return False, "通知模块不可用"
    try:
        ok, msg = _notify.send(flatten(ch), row["title"], row["text"])
    except Exception as e:  # noqa: BLE001
        ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
    storage.channel_touch(ch["id"], ok, "" if ok else msg, ts)
    if ok:
        storage.outbox_mark(oid, "done")
    else:
        storage.outbox_mark(oid, "pending", msg, ts + RETRY_BACKOFF[0])
    return ok, msg


def push_digest(storage, hours: int = 24, channel_ids: list[str] | None = None,
                ts: int = 0) -> dict:
    """生成巡检摘要并推送（定时调度与 UI 手动推送共用）。"""
    import time

    from . import report
    ts = ts or int(time.time())
    title, text = report.digest_text(storage, hours, ts)
    chans = {c["id"]: c for c in storage.list_channels() if c["enabled"]}
    ids = channel_ids or list(chans)
    channels = [chans[c] for c in ids if c in chans]
    delivered, n_ch, n_ok, err, failed = _dispatch(storage, channels, title, text, ts)
    for ch, msg in failed:
        storage.outbox_add(ts, 0, ch["id"], title, text, ts + RETRY_BACKOFF[0], err=msg)
    storage.setting_set("digest_last_ts", str(ts))
    return {"title": title, "channels": n_ch, "ok": n_ok, "delivered": delivered, "error": err}


# 评估串行化：evaluate 有两个入口（后台 30s 循环 + POST /api/alerts/evaluate 手动触发），
# 内部「读告警最新行 → 算 → 写告警行」跨多次独立持锁事务，并发同刻会对同一规则+目标
# 发两条 firing/写两行 alerts（silence 期只隔开两次完整评估，拦不住并发同刻）。
_EVAL_LOCK = threading.Lock()


def evaluate(storage, ts: int = 0) -> list[dict]:
    """评估一轮全部启用规则；返回本轮产生的事件列表（firing / remind / resolved）。"""
    with _EVAL_LOCK:
        return _evaluate_impl(storage, ts)


# 动态基线预警日志限频（rule_id -> 上次预警时间）：预警只进日志，别把日志刷爆
_ANOMALY_WARN_AT: dict = {}


def _anomaly_changes(storage, rule: dict, task: dict, label: str, ts: int) -> list[dict]:
    """anomaly 规则评估（第十期）：动态基线偏离，产出与主循环同构的 changes。

    与阈值路径的差异：不走 window_stats（阈值语义不适用），判定来自 baseline.baseline()
    的流级 z 序列；firing/resolved 仍走同一套告警状态机（alerts 追加行/silence/重投全复用）。
    另含 5m 快路径「预警」：只 log.warning，不改状态机（设计文档 §五 双层判定）。
    """
    from . import baseline as _bl
    p = _bl.params_of(rule)
    task_id = str(task["id"])
    try:
        res = _bl.baseline(storage, task_id, p, ts)
    except Exception as e:  # noqa: BLE001 - 基线计算失败绝不影响其它规则评估
        log.warning("动态基线计算失败 rule=%s task=%s: %s", rule["name"], task_id, e)
        return []
    if not res.get("evaluable"):
        # 冷启动/样本不足：如实说明，限频记日志（每规则 10 分钟最多一条）
        last_warn = _ANOMALY_WARN_AT.get(rule["id"], 0)
        if ts - last_warn > 600:
            _ANOMALY_WARN_AT[rule["id"]] = ts
            reason = next((s.get("reason") for s in res.get("streams", []) if s.get("reason")),
                          "样本不足")
            log.info("动态基线未评估 rule=%s task=%s：%s（基线就绪前由阈值规则兜底）",
                     rule["name"], task_id, reason)
        return []

    fired = [s for s in res["streams"] if s.get("fired")]
    key = task_id
    opened = storage.alert_open(rule["id"], key)
    last = storage.alert_last(rule["id"], key)

    # 5m 快路径预警：当前 5m 桶按同一基线偏离超 k → 日志提醒（不改告警状态机）
    if not fired:
        try:
            mf = res["metric_field"]
            t5 = ts // 300 * 300 - 300
            for r in storage.agg_metric_values(task_id, "5m", mf, t5, t5):
                s0 = next((s for s in res["streams"]
                           if s["node_id"] == r["node_id"] and s["dns"] == r["dns"]
                           and s["url"] == r["url"] and s.get("evaluable")), None)
                if not s0 or r["value"] is None or not s0.get("scale"):
                    continue
                z5 = (float(r["value"]) - s0["center"]) / s0["scale"]
                if abs(z5) >= (s0.get("eff_k") or res["k"]) and \
                        (res["direction"] != "down" or z5 <= 0) and \
                        (res["direction"] != "up" or z5 >= 0):
                    last_warn = _ANOMALY_WARN_AT.get(rule["id"], 0)
                    if ts - last_warn > 600:
                        _ANOMALY_WARN_AT[rule["id"]] = ts
                        log.warning("动态基线预警（未确认）: %s 流 %s 当前值 %.2f 偏离基线 "
                                    "%.2f±%.2f 达 %.1fkσ——等 1h 桶确认", rule["name"],
                                    (r["node_id"], r["dns"], r["url"]), float(r["value"]),
                                    s0["center"], s0["scale"], abs(z5))
                    break
        except Exception:  # noqa: BLE001 - 预警绝不影响正式评估
            pass

    # 当前桶偏离度（滞回判定）：恢复=当前桶 |z| < 0.8k，而非「连续序列仍偏离」——
    # 否则前一小时的历史偏离会挡住恢复
    evaluable_streams = [s for s in res["streams"] if s.get("evaluable")]
    cur_dev = max((abs(s["zs"][-1][2]) for s in evaluable_streams
                   if s.get("zs") and s["zs"][-1][2] is not None), default=None)
    # 状态语义：进入=连续 mc 桶偏离（fired）；维持=当前桶 |z| ≥ eff_k；恢复=当前桶
    # |z| < 0.8*eff_k（滞回）。eff_k：zscore=k / bounded=1.0（流级 st["eff_k"]）——
    # 用 k 会让 bounded 在 z∈[1.0,0.8k) firing↔resolved 振铃（复核 P1 修正）
    _eff_k = max((s.get("eff_k") or res["k"]) for s in res["streams"]
                 if s.get("evaluable")) if res["streams"] else res["k"]
    cur_deviant = bool(cur_dev is not None and cur_dev >= _eff_k)
    in_firing = bool(opened and last and last["status"] == "firing")
    if in_firing and cur_dev is not None and cur_dev < 0.8 * _eff_k:
        kind = "resolved"                      # 滞回恢复
    elif fired:
        if in_firing and (ts - int(last["ts"] or 0)) < int(rule["silence_seconds"] or 0):
            return []                          # 持续偏离，静默期内不打扰
        kind = "remind" if in_firing else "firing"
    elif in_firing:
        return []                              # 0.8*eff_k~eff_k 滞回带内：维持 firing 不发通知
    else:
        return []                              # 无未恢复告警且未满足进入条件

    # 展示流：firing/remind 取最劣流；resolved 取首个可评估流
    pool = fired or [s for s in res["streams"] if s.get("evaluable")]
    worst = max(pool, key=lambda s: abs(s.get("worst_z") or 0)) if kind != "resolved" \
        else pool[0]
    where = " · ".join(x for x in (worst["node_id"], worst.get("dns") or "",
                                   worst.get("url") or "") if x)
    head = {"firing": "【告警】", "remind": "【提醒】", "resolved": "【恢复】"}[kind]
    title = head + "动态基线偏离 · " + str(rule["name"])
    zs_txt = "；".join("当前 %.2f（%s）" % (v, _time_text(t)) for t, v, _ in worst.get("zs", [])
                       if v is not None) or "当前值缺失"
    body = "\n".join([
        "- 规则：" + str(rule["name"]) + "（动态基线偏离）",
        "- 任务：" + label,
        "- 基线：" + res.get("window_desc", ""),
        ("- 判定：偏离 %skσ ≥ 阈值 %skσ，连续 %d 个小时桶"
         % (worst.get("worst_z") if worst.get("worst_z") is not None else 0,
            res.get("k"), int(res.get("mc") or 0))) if kind != "resolved"
        else "- 判定：已回到基线带宽内（滞回恢复）",
        "- 最劣流：" + where,
        "- " + zs_txt,
        "- 基线数字：中位 %s（MAD %s，样本 %d）" % (
            worst.get("center"), worst.get("scale"), worst.get("samples")),
    ])
    target = {"task_id": task_id, "node_id": worst.get("node_id") or "", "label": label,
              "value": worst.get("cur_v"), "threshold": res.get("k"),
              "z": worst.get("worst_z"), "samples": worst.get("samples")}
    return [{"rule": rule, "key": key, "label": label, "kind": kind,
             "title": title, "text": body, "target": target}]


def _evaluate_impl(storage, ts: int = 0) -> list[dict]:
    import time
    ts = ts or int(time.time())
    out: list[dict] = []
    rules = [r for r in storage.list_rules() if r["enabled"]]
    if not rules:
        return out
    chans = {c["id"]: c for c in storage.list_channels() if c["enabled"]}
    tasks = {t["id"]: t for t in storage.list_tasks()}
    nodes = {n["id"]: n for n in storage.list_nodes()}
    node_names = {nid: str(n.get("name") or nid) for nid, n in nodes.items()}
    changes: list[dict] = []

    for rule in rules:
        metric = rule["metric"]
        if metric == "anomaly":
            # 动态基线（第十期）：不走 window_stats 阈值路径，判定在 baseline.baseline()；
            # 创建时已校验必须指定任务（任务级基线，按流判定）
            t_id = str(rule["task_id"] or "")
            task = tasks.get(t_id)
            if not task or not task.get("enabled"):
                continue
            if storage.in_maintenance(ts, task_id=t_id):
                continue
            changes.extend(_anomaly_changes(storage, rule, task,
                                            task.get("name") or t_id, ts))
            continue
        keys: list[tuple[str, str]] = []          # [(key, label)]
        if metric == "node_offline":
            ids = [rule["node_id"]] if rule["node_id"] else list(nodes)
            keys = [(nid, nodes.get(nid, {}).get("name", nid)) for nid in ids if nid in nodes]
        else:
            ids = [rule["task_id"]] if rule["task_id"] else [t["id"] for t in tasks.values() if t["enabled"]]
            keys = [(tid, tasks[tid]["name"]) for tid in ids if tid in tasks]

        for key, label in keys:
            t_id = key if metric != "node_offline" else ""
            n_id = key if metric == "node_offline" else ""
            if storage.in_maintenance(ts, task_id=t_id, node_id=n_id):
                continue

            stats = {}
            if metric == "node_offline":
                online = (nodes.get(key, {}).get("status") == "online")
                value: float | None = 0.0 if online else 1.0
            else:
                stats = window_stats(storage, key, rule["window_seconds"], ts)
                value = stats.get(metric if metric != "loss" else "loss")
            firing = _compare(value, rule["op"], float(rule["threshold"]))
            opened = storage.alert_open(rule["id"], key)

            if firing:
                last = storage.alert_last(rule["id"], key)
                if last and last["status"] == "firing" and \
                        (ts - int(last["ts"] or 0)) < int(rule["silence_seconds"] or 0):
                    continue                     # 静默期内不重复打扰
                kind = "remind" if opened else "firing"
            else:
                if not opened:
                    continue                     # 无未恢复告警且条件不满足 → 无需动作
                kind = "resolved"

            title, text = _fmt(rule, label, value, stats, ts, kind)
            if kind != "resolved":
                # 告警即诊断：firing/remind 追加固定段落；resolved 保持原样（向后兼容）
                try:
                    extra = _diagnosis_lines(storage, rule, key, label, stats, ts, node_names)
                except Exception:  # noqa: BLE001 - 诊断段落绝不影响告警主流程
                    extra = []
                if extra:
                    text = text + "\n" + "\n".join(extra)
            target = {"task_id": t_id, "node_id": n_id, "label": label,
                      "value": _scaled(metric, value),
                      "threshold": _scaled(metric, rule["threshold"]),
                      "count": stats.get("count"), "ok": stats.get("ok"),
                      "fail": stats.get("fail")}
            changes.append({"rule": rule, "key": key, "label": label, "kind": kind,
                            "title": title, "text": text, "target": target})

    # ---- 聚合派发：同一条规则同一轮里的多个目标合并成一条通知，避免「多节点/多线路刷屏」----
    groups: dict = {}
    for ch in changes:
        dkind = "resolved" if ch["kind"] == "resolved" else "firing"
        groups.setdefault((ch["rule"]["id"], dkind), []).append(ch)

    for (rid, dkind), items in groups.items():
        rule = items[0]["rule"]
        channels = [chans[c] for c in rule["channel_ids"] if c in chans]
        if len(items) == 1:
            send_title, send_text = items[0]["title"], items[0]["text"]
        else:
            head = "【告警】" if dkind == "firing" else "【恢复】"
            names = "、".join(str(i["label"]) for i in items[:6])
            more = "" if len(items) <= 6 else (" 等 " + str(len(items)) + " 个目标")
            send_title = head + rule["name"] + " · " + str(len(items)) + " 个目标同时" + \
                ("异常" if dkind == "firing" else "恢复")
            lines = ["- 规则：" + rule["name"] + "（" + METRICS.get(rule["metric"], (rule["metric"],))[0] + "）",
                     "- 影响：" + str(len(items)) + " 个目标 —— " + names + more,
                     "- 时间：" + _time_text(ts), ""]
            for i in items:
                lines.append("- " + str(i["label"]) + "：" + i["title"].split("】")[-1])
            send_text = "\n".join(lines)
        delivered, n_ch, n_ok, err, failed = _dispatch(storage, channels, send_title, send_text, ts)
        for i in items:
            note = err
            if len(items) > 1:
                note = (note + "；" if note else "") + "聚合发送(" + str(len(items)) + " 个目标)"
            aid = storage.alert_add(ts, rule, i["key"],
                                    "firing" if i["kind"] != "resolved" else "resolved",
                                    i["title"], i["text"], i["target"],
                                    delivered, n_ch, n_ok, note)
            out.append({"id": aid, "kind": i["kind"], "rule": rule["name"], "metric": rule["metric"],
                        "key": i["key"], "label": i["label"], "title": i["title"],
                        "delivered": delivered, "channels": n_ch, "ok": n_ok, "error": err,
                        "grouped": len(items)})
            log.info("告警 %s: %s（%d 个目标聚合，渠道 %d/%d 成功）",
                     i["kind"], i["title"], len(items), n_ok, n_ch)
        # 派发失败的渠道进重投队列（按退避重试，失败也不影响评估）
        for ch, msg in failed:
            storage.outbox_add(ts, 0, ch["id"], send_title, send_text,
                               ts + RETRY_BACKOFF[0], err=msg)

    # ---- 升级链：firing 超过 escalate_minutes 未确认 → 【升级】重新通知 ----
    try:
        out.extend(_escalations(storage, rules, chans, tasks, nodes, node_names, ts))
    except Exception as e:  # noqa: BLE001 - 升级链绝不影响评估主流程
        log.warning("升级链扫描失败: %s: %s", type(e).__name__, e)
    return out


def _open_incidents(storage) -> list[dict]:
    """未恢复事件列表（升级链判断 ack 状态用；只读、带 LIMIT）。"""
    out = _safe_call(storage, "list_incidents", default=[], limit=100, open_only=True)
    return out if isinstance(out, list) else []


def _has_unacked_incident(rule_metric: str, key: str, incidents: list[dict]) -> bool:
    """升级继续条件：存在该告警关联的、仍未确认（acked_at=0）的未恢复事件。

    - metric=node_offline：按 node_id 关联；其余指标按 task_id 关联；
    - 关联事件全部已确认 → 停止升级（人已处理）；
    - 没有关联的未恢复事件（已随恢复关闭，或阈值型告警未开单）→ 停止升级：
      升级链的停止条件是「人已确认」或「条件恢复」，恢复中的窗口滞后不再打扰。
    """
    if not incidents:
        return False
    field = "node_id" if rule_metric == "node_offline" else "task_id"
    related = [i for i in incidents if str(i.get(field) or "") == str(key)]
    return any(int(i.get("acked_at") or 0) <= 0 for i in related)


def _escalations(storage, rules: list[dict], chans: dict, tasks: dict, nodes: dict,
                 node_names: dict, ts: int) -> list[dict]:
    """升级链扫描（evaluate 每轮调用）：到期未确认的 firing 告警重新通知。

    触发条件（全部满足）：
    - 规则配置 escalate_minutes > 0；
    - 该规则+目标仍处于 firing（最新一条 alerts 行是 firing）；
    - 本轮 firing 持续时长（本轮起点 = 最近一次 resolved 之后的首条 firing）≥ 阈值；
    - 距上次升级 ≥ escalate_minutes（间隔不小于阈值；上次升级时间持久化在升级行
      alerts.detail 的 "escalated_at=<ts>"，重启不丢）；
    - 仍存在未确认的关联未恢复事件（全部已确认或已随恢复关闭 → 停止升级）。

    升级行本身 status='firing'（保持 alert_open/alert_last 的「最新行」语义），
    title 以【升级】为前缀，通知走同一规则的同一批渠道。
    """
    out: list[dict] = []
    incidents: list[dict] | None = None
    for rule in rules:
        em = _escalate_minutes(rule)
        if em <= 0:
            continue
        metric = rule["metric"]
        keys = _safe_call(storage, "alert_open_keys", rule["id"], default=[]) or []
        for key in keys:
            t0 = int(_safe_call(storage, "alert_episode_start", rule["id"], key, default=0) or 0)
            if t0 <= 0 or (ts - t0) < em * 60:
                continue                     # 未到升级阈值
            last = int(_safe_call(storage, "alert_last_escalated", rule["id"], key, default=0) or 0)
            if last and (ts - last) < em * 60:
                continue                     # 间隔不小于 escalate_minutes
            if incidents is None:
                incidents = _open_incidents(storage)
            if not _has_unacked_incident(metric, key, incidents):
                continue                     # 全部已确认 / 无未恢复事件 → 不再升级
            if metric == "node_offline":
                label = node_names.get(key, key)
                stats: dict = {}
                value: float | None = 0.0 if nodes.get(key, {}).get("status") == "online" else 1.0
                mname, munit = "节点离线", ""
                title = "【升级】节点 " + label + " 离线已持续 " + str((ts - t0) // 60) + " 分钟未确认"
            else:
                label = str(tasks.get(key, {}).get("name") or key)
                stats = window_stats(storage, key, rule["window_seconds"], ts)
                value = stats.get(metric)
                mname, _d, munit, _agg = METRICS.get(metric, (metric, "gt", "", True))
                title = "【升级】" + label + " " + mname + " 已持续 " + str((ts - t0) // 60) + " 分钟未确认"
            shown = _scaled(metric, value)
            body = [
                "- 规则：" + rule["name"] + "（" + mname + "）",
                "- 目标：" + label,
                "- 当前值：" + (str(shown) + munit if shown is not None else "无数据"),
                "- 时间：" + _time_text(ts),
            ]
            try:
                extra = _diagnosis_lines(storage, rule, key, label, stats, ts, node_names)
            except Exception:  # noqa: BLE001
                extra = []
            text = "\n".join(body + extra)
            channels = [chans[c] for c in rule["channel_ids"] if c in chans]
            delivered, n_ch, n_ok, err, failed = _dispatch(storage, channels, title, text, ts)
            target = {"task_id": "" if metric == "node_offline" else key,
                      "node_id": key if metric == "node_offline" else "",
                      "label": label, "value": _scaled(metric, value),
                      "threshold": _scaled(metric, rule["threshold"]),
                      "escalated": True}
            aid = storage.alert_add(ts, rule, key, "firing", title, text, target,
                                    delivered, n_ch, n_ok, "escalated_at=" + str(ts))
            for ch, msg in failed:
                storage.outbox_add(ts, aid, ch["id"], title, text,
                                   ts + RETRY_BACKOFF[0], err=msg)
            out.append({"id": aid, "kind": "escalate", "rule": rule["name"], "metric": metric,
                        "key": key, "label": label, "title": title, "delivered": delivered,
                        "channels": n_ch, "ok": n_ok, "error": err, "grouped": 1})
            log.info("告警升级: %s（渠道 %d/%d 成功）", title, n_ok, n_ch)
    return out


def test_channel(storage, channel: dict, ts: int = 0) -> tuple[bool, str]:
    """手动测试发送（供 UI「测试发送」按钮）。"""
    import time
    ts = ts or int(time.time())
    title = "【测试】gpm 通知渠道连通性"
    text = "\n".join([
        "- 渠道：" + channel.get("name", channel.get("id", "")),
        "- 时间：" + _time_text(ts),
        "- 说明：收到本条即表示该渠道配置可用。",
    ])
    if _notify is None:
        return False, "通知模块（notify.py）不可用"
    try:
        ok, msg = _notify.send(flatten(channel), title, text)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, "异常: " + type(e).__name__ + ": " + str(e)[:120]
    storage.channel_touch(channel["id"], ok, "" if ok else msg, ts)
    return ok, msg
