"""JEV 故障判断（.docs/ONCALL_OPTIMIZATION_2.md 第七期 31-38）。

参考 G:\\ai_project\\JEV测试\\jev高考题测试 的核心不是「让模型写解释」，而是四件事：
代码拥有全部控制流 + 模型只提供**类型化判断** + 证据由**代码切分候选** + 一致性/依据薄弱判定。
移植到故障定位恰好对症：**运维最怕的不是「没结论」，而是「一个看起来很像结论的幻觉」**。

几条硬约束（违反任何一条，本期的设计就塌了）：

1. **证据候选由代码切分**：候选证据（E1..En）从真实数据产出，带稳定 id。
   模型**只能从候选里选**，引用池外的 id 一律判无效——这是反幻觉的地基。
2. **固定假设集**：DNS / 网络 / 端口 / TLS / 服务端 / 应用内容 / 节点侧 / 第三方依赖 /
   我方平台自身。逐假设独立判断，只回 support（概率）+ confidence，**不回解释性长文**。
3. **代码拥有控制流**：阈值、最终结论、分歧判定、依据强弱判定全在本模块的常量与函数里。
   调阈值只改代码；同输入同阈值结论稳定。
4. **一致性三态**：规则链 diagnose.classify（确定性） vs JEV 假设判断（概率） →
   一致 / 存在分歧 / **依据薄弱**（最高支持度 < 阈值 → 明说「证据不足，人工判读」，绝不编根因）。
5. **判据可插拔**：Judge 是接口。LocalJudge 是本机确定性判据（不依赖任何模型，可测可复现）；
   HttpJudge 把 fan-out 载荷发给配置的 LLM 网关并读回**类型化判断**。
   **HttpJudge 未对真实模型验证过**（本机没有模型凭据）——这是如实记录的限制，不假装已验证。
"""
from __future__ import annotations

import json
import time

from ..config import Config
from .diagnose import classify, runbook_for

# ---------------------------------------------------------------- 控制流常量（都在代码里）

HYPOTHESES = ("DNS", "网络", "端口", "TLS", "服务端", "应用内容", "节点侧",
              "第三方依赖", "我方平台自身")

# —— 以下阈值由 cfg.jev.* 提供；保留模块级同名常量供 tests / 旧调用按属性名直接读取。
# 默认值与 src/gpm/config.py DEFAULTS["jev"] 对齐；运行期 init(cfg) 会覆盖。
MIN_SUPPORT = 0.5
WEAK_SUPPORT = 0.5
DISAGREE_MARGIN = 0.15
MIN_CONFIDENCE = 0.3

_cfg: Config | None = None


def init(cfg) -> None:
    """由 app 在启动时注入 cfg；之后 MIN_SUPPORT / WEAK_SUPPORT / DISAGREE_MARGIN /
    MIN_CONFIDENCE 同步到 cfg.jev.*。tests 仍可临时改这几个模块级属性。"""
    global _cfg, MIN_SUPPORT, WEAK_SUPPORT, DISAGREE_MARGIN, MIN_CONFIDENCE
    _cfg = cfg
    try:
        MIN_SUPPORT = float(cfg.jev.get("min_support", MIN_SUPPORT))
    except Exception:
        pass
    try:
        WEAK_SUPPORT = float(cfg.jev.get("weak_support", WEAK_SUPPORT))
    except Exception:
        pass
    try:
        DISAGREE_MARGIN = float(cfg.jev.get("disagree_margin", DISAGREE_MARGIN))
    except Exception:
        pass
    try:
        MIN_CONFIDENCE = float(cfg.jev.get("min_confidence", MIN_CONFIDENCE))
    except Exception:
        pass

EVIDENCE_HINT = {
    "error_class": ("DNS", "网络", "端口", "TLS", "服务端", "应用内容"),
    "scope": (),
    "change": ("我方平台自身",),
    "dns_change": ("DNS",),
    "node_resource": ("节点侧",),
    "node_heartbeat": ("节点侧",),
}

RULE_LAYER_TO_ROOT = {
    "DNS 层": "DNS", "网络层": "网络", "网络/端口层": "端口",
    "网络/服务端层": "服务端", "TLS 层": "TLS", "服务端层": "服务端",
    "应用层": "应用内容", "节点侧": "节点侧", "节点侧(DNS)": "DNS",
}


# ---------------------------------------------------------------- 证据池（代码切分）

def build_evidence(detail: dict, storage=None) -> list:
    """从事件详情（人类在弹窗里看到的同一份数据）切出候选证据，带稳定 id。

    这一步必须是**纯代码**：模型只能从这里挑，不能自己编引文。
    每条证据都有 kind（决定它的倾向性假设）与 text（人类可读的一句话）。
    """
    ev: list = []
    inc = detail.get("incident") or {}
    reason = inc.get("reason") or {}
    ec = str(reason.get("error_class") or "")

    def push(kind: str, text: str, strong: bool):
        ev.append({"id": "E%d" % (len(ev) + 1), "kind": kind,
                   "text": text[:160], "strong": strong})

    if ec:
        layer, advice = classify(ec)
        push("error_class", "error_class=%s（规则链判为「%s」：%s）" % (ec, layer, advice), True)
    err = str(reason.get("error") or "")
    if err:
        push("error_class", "原始错误：%s" % err[:120], False)

    sm = detail.get("scope_matrix") or {}
    verdict = sm.get("verdict") or {}
    if verdict.get("verdict"):
        push("scope", "范围：%s" % verdict["verdict"], True)

    for c in (detail.get("changes") or [])[:3]:
        if not c.get("action") or c.get("action") == "none":
            continue
        push("change", "同期变更：%s · %s" % (c.get("action"), c.get("detail")), True)

    for d in (detail.get("dns_changes") or [])[:2]:
        if not (d.get("answers") or []):
            continue
        push("dns_change", "DNS 答案变更：%s" % json.dumps(d.get("answers"), ensure_ascii=False)[:120], True)

    for p in (detail.get("dying") or [])[-3:]:
        if p.get("cpu") is None and p.get("mem") is None:
            continue
        push("node_resource", "节点心跳资源：cpu=%s mem=%s" % (p.get("cpu"), p.get("mem")), False)

    # 基线偏离（第十期）：同任务近 1h 的 anomaly firing 告警。z≥2k 记 strong——
    # 偏离远超判定阈值时，这比 error_class 更能说明「是真的变了」而非抖动。
    if storage is not None:
        tid = str(inc.get("task_id") or "")
        if tid:
            try:
                for a in (storage.recent_anomaly_alerts(tid, max(0, int(inc.get("started_at") or 0) - 3600)) or [])[:2]:
                    z = a.get("z")
                    strong = bool(z is not None and abs(float(z)) >= 6)
                    push("baseline", "动态基线偏离告警：%s（偏离 %skσ）"
                         % (a.get("rule_name") or "?", z if z is not None else "?"), strong)
            except Exception:
                pass  # 证据查询失败不影响证据池其余部分

    if not inc.get("task_id"):
        push("node_heartbeat", "节点侧事件：心跳中断（任务侧无目标）", True)
    return ev


# ---------------------------------------------------------------- 判据（可插拔）

class LocalJudge:
    """本机确定性判据：不依赖任何模型，同输入同输出，可测可复现。

    它做的正是「模型该做的事」——对每个假设给一个 support/confidence——只不过由
    代码按证据的倾向性加权算出。这样即使**没有模型**，整条控制流（候选池、假设集、
    一致性三态、依据薄弱）也完整可跑、可测。
    """
    name = "local"

    def judge(self, evidence: list, hypotheses: tuple, rule_hint: dict) -> list:
        hints: dict = {}
        for e in evidence:
            if not e.get("strong"):
                continue
            for h in EVIDENCE_HINT.get(e["kind"], ()):
                hints[h] = hints.get(h, 0.0) + 1.0
        total = sum(hints.values()) or 1.0
        n_strong = sum(1 for e in evidence if e.get("strong"))
        out = []
        for h in hypotheses:
            support = round(min(0.99, hints.get(h, 0.0) / total), 3)
            confidence = round(min(0.95, 0.35 + 0.15 * n_strong), 3) if support > 0 else 0.1
            out.append({"hypothesis": h, "support": support, "confidence": confidence})
        return out


class HttpJudge:
    """把 fan-out 载荷发给配置的 LLM 网关，读回**类型化判断**。

    未对真实模型验证过（本机没有模型凭据）——如实记录。它只负责「取判断」，
    **不参与任何阈值/结论/分歧判定**：那些仍然全在代码里。
    """
    name = "http"

    def __init__(self, url: str, token: str = "", timeout: int = 20):
        self.url = url
        self.token = token
        self.timeout = timeout

    def judge(self, evidence: list, hypotheses: tuple, rule_hint: dict) -> list:
        import urllib.request
        body = json.dumps({
            "evidence": [{"id": e["id"], "kind": e["kind"], "text": e["text"]} for e in evidence],
            "hypotheses": list(hypotheses),
            "hint": rule_hint,
            "contract": "只回 [{hypothesis, support, confidence, cited?}]，不要解释文字",
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
        return raw


# ---------------------------------------------------------------- 控制流（代码拥有）

def normalize_judgments(raw, hypotheses: tuple, evidence_ids: list) -> tuple:
    """把判据输出规整成类型化判断；越界/缺字段一律丢弃并记录原因。

    返回 (有效判断, 被拒原因列表)。**模型无法用「写一篇解释」绕过这里**。
    """
    good: list = []
    rejected: list = []
    if not isinstance(raw, list):
        return [], ["输出不是列表"]
    valid_ids = set(evidence_ids)
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            rejected.append("第 %d 条不是对象" % i)
            continue
        h = str(item.get("hypothesis") or "")
        if h not in hypotheses:
            rejected.append("第 %d 条假设不在固定集合内：%s" % (i, h))
            continue
        try:
            # 显式判空：get 返回 None 时 float(None) 抛 TypeError，但 mypy 只看类型
            sp = item.get("support")
            cf = item.get("confidence")
            if sp is None or cf is None:
                raise ValueError("missing")
            support = float(sp)
            confidence = float(cf)
        except (TypeError, ValueError):
            rejected.append("第 %d 条缺 support/confidence" % i)
            continue
        if not (0.0 <= support <= 1.0 and 0.0 <= confidence <= 1.0):
            rejected.append("第 %d 条概率越界" % i)
            continue
        cited = item.get("cited") or []
        if cited:
            bad = [c for c in cited if str(c) not in valid_ids]
            if bad:
                rejected.append("第 %d 条引用了池外证据：%s" % (i, ",".join(map(str, bad))))
                continue
        good.append({"hypothesis": h, "support": round(support, 3),
                     "confidence": round(confidence, 3)})
    return good, rejected


def decide(judgments: list, rule_hint: dict, cfg=None) -> dict:
    """**代码拥有控制流**：阈值、结论、分歧、依据强弱全在这里，模型不参与。

    cfg 留空时用模块级常量（由 init(cfg) 同步为 cfg.jev.*）。
    """
    weak_support = WEAK_SUPPORT
    min_confidence = MIN_CONFIDENCE
    disagree_margin = DISAGREE_MARGIN
    if cfg is not None:
        try:
            weak_support = float(cfg.jev.get("weak_support", weak_support))
        except Exception:
            pass
        try:
            min_confidence = float(cfg.jev.get("min_confidence", min_confidence))
        except Exception:
            pass
        try:
            disagree_margin = float(cfg.jev.get("disagree_margin", disagree_margin))
        except Exception:
            pass
    if not judgments:
        return {"state": "weak", "root_cause": None,
                "note": "判据没有给出任何有效判断，证据不足，人工判读"}
    ranked = sorted(judgments, key=lambda x: (-x["support"], -x["confidence"]))
    top, second = ranked[0], (ranked[1] if len(ranked) > 1 else None)
    if top["support"] < weak_support or top["confidence"] < min_confidence:
        return {"state": "weak", "root_cause": None,
                "note": "最高支持度 %.2f / 置信度 %.2f 低于阈值（%.2f / %.2f），"
                        "证据不足，人工判读 —— 不编根因"
                        % (top["support"], top["confidence"], weak_support, min_confidence)}
    margin = top["support"] - (second["support"] if second else 0.0)
    if second and margin < disagree_margin:
        return {"state": "diverge", "root_cause": top["hypothesis"],
                "note": "最高与次高支持度只差 %.2f（< %.2f），存在分歧，建议人工复核"
                        % (margin, disagree_margin)}
    return {"state": "agree", "root_cause": top["hypothesis"],
            "note": "最高支持度 %.2f、置信度 %.2f，且领先次高 %.2f"
                    % (top["support"], top["confidence"], margin)}


def consistency(rule_root: str, decision: dict) -> str:
    """一致性三态：规则链（确定性） vs JEV 判断（概率）。规则结论永不被模型覆盖。"""
    if decision["state"] == "weak":
        return "依据薄弱"
    rc = decision.get("root_cause")
    if not rc:
        return "依据薄弱"
    if rule_root and rc != rule_root:
        return "存在分歧"
    return "一致"


# ---------------------------------------------------------------- 主流程

def run(detail: dict, judge=None, ts: int | None = None, cfg=None, storage=None) -> dict:
    """对一个事件跑一次 JEV 判断，返回可回放的轨迹。

    **前置门禁（第七期 38）**：调用方必须先确认「不可信事件数 = 0」，
    否则输入本身是僵尸/陈旧证据，模型只会把噪声包装成结论。

    cfg 留空时使用模块级常量（由 init(cfg) 同步为 cfg.jev.*）。
    """
    t0 = int(ts or time.time())
    evidence = build_evidence(detail, storage=storage)
    inc = detail.get("incident") or {}
    rule_layer, rule_advice = classify(str((inc.get("reason") or {}).get("error_class") or ""))
    rule_root = RULE_LAYER_TO_ROOT.get(rule_layer, "")

    j = judge or LocalJudge()
    judgments, rejected = normalize_judgments(
        j.judge(evidence, HYPOTHESES, {"rule_root": rule_root}),
        HYPOTHESES, [e["id"] for e in evidence])
    decision = decide(judgments, {"rule_root": rule_root}, cfg=cfg)
    state = consistency(rule_root, decision)

    # rule 字段回显当前生效的阈值（取自 cfg 或模块级常量）
    rule_dict = {"min_support": MIN_SUPPORT, "weak_support": WEAK_SUPPORT,
                 "disagree_margin": DISAGREE_MARGIN, "min_confidence": MIN_CONFIDENCE,
                 "model_can_override_rule": False}
    if cfg is not None:
        try:
            rule_dict["min_support"] = float(cfg.jev.get("min_support", MIN_SUPPORT))
            rule_dict["weak_support"] = float(cfg.jev.get("weak_support", WEAK_SUPPORT))
            rule_dict["disagree_margin"] = float(cfg.jev.get("disagree_margin", DISAGREE_MARGIN))
            rule_dict["min_confidence"] = float(cfg.jev.get("min_confidence", MIN_CONFIDENCE))
        except Exception:
            pass

    return {
        "incident_id": inc.get("id"),
        "ts": t0, "judge": getattr(j, "name", "local"),
        "evidence": evidence,
        "judgments": judgments,
        "rejected": rejected,
        "rule": rule_dict,
        "verdict": {
            "state": state,
            "rule_conclusion": {"layer": rule_layer, "advice": rule_advice,
                                "runbook": runbook_for(rule_layer), "root": rule_root},
            "jev_conclusion": decision,
            "note": decision["note"],
        },
        "total_ms": max(0, int(time.time() * 1000) - t0 * 1000),
    }


# ---------------------------------------------------------------- 存储

def save(storage, trace: dict) -> int:
    with storage.lock:
        storage.db.execute(
            "INSERT OR REPLACE INTO jev_traces(incident_id,ts,judge,evidence_json,"
            "judgments_json,rule_json,verdict_json,total_ms) VALUES(?,?,?,?,?,?,?,?)",
            (int(trace["incident_id"]), int(trace.get("ts") or 0),
             str(trace.get("judge") or ""),
             json.dumps(trace.get("evidence") or [], ensure_ascii=False),
             json.dumps({"judgments": trace.get("judgments") or [],
                         "rejected": trace.get("rejected") or []}, ensure_ascii=False),
             json.dumps(trace.get("rule") or {}, ensure_ascii=False),
             json.dumps(trace.get("verdict") or {}, ensure_ascii=False),
             int(trace.get("total_ms") or 0)))
        storage.db.commit()
    return int(trace["incident_id"])


def load(storage, incident_id: int):
    with storage.lock:
        row = storage.db.execute("SELECT * FROM jev_traces WHERE incident_id=?",
                                 (int(incident_id),)).fetchone()
    if not row:
        return None
    d = dict(row)
    j = json.loads(d.pop("judgments_json") or "{}")
    return {
        "incident_id": d["incident_id"], "ts": d["ts"], "judge": d["judge"],
        "evidence": json.loads(d.pop("evidence_json") or "[]"),
        "judgments": j.get("judgments") or [], "rejected": j.get("rejected") or [],
        "rule": json.loads(d.pop("rule_json") or "{}"),
        "verdict": json.loads(d.pop("verdict_json") or "{}"),
        "total_ms": d["total_ms"],
    }
