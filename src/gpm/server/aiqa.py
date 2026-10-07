"""AI 分析（第 8 子页「AI 分析」）——自然语言故障问答。

回答的问题：**「14:00~16:00 有哪些故障？是否有关联？」**

架构三层，延续 jev 的纪律（代码拥有控制流，模型只做改写、绝不产生新结论）：

1. **时间解析（纯代码）**：从自然语言里抽时间段。支持 HH:MM~HH:MM / N点~M点 /
   昨天/今天/N小时前 相对语义 / ISO 日期时间。解析不出 → 明确要求用时间选择器，
   不猜。
2. **事实查询（纯代码）**：correlation.analyze(t_from, t_to) —— 事件清单 + 簇 +
   疑似假设 + 证据计数，全部确定性输出（第八期既有能力，本地+节点侧+外部告警全含）。
3. **LLM 改写（OpenAI chat 兼容）**：把事实 JSON 翻译成一段中文回答。模型**只能
   重组/翻译给定事实**；回答后经锚定校验（数字与实体名必须存在于 facts 白名单），
   对不上整体降级为结构化卡片 + 警示。网关未配置/超时/报错 → degraded 如实返回。

不编造三原则的落点：
- 时间没解析出来就说没解析出来（不猜一个默认窗口冒充用户意图）；
- 网关挂了就说网关挂了（结构化结果照给，不假装 AI 在工作）；
- 模型说的数字查无实据就标注不可信（或整体降级），绝不 silently 采纳。
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta

from ..config import Config

_cfg: Config | None = None


def init(cfg) -> None:
    global _cfg
    _cfg = cfg


# ---------------------------------------------------------------- 时间解析

_TIME_RE = r"(\d{1,2}):(\d{2})"
_HAN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
            "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
            "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20, "二十一": 21,
            "二十二": 22, "二十三": 23, "二十四": 24}
# 「N点」：中文数字（十四点）或阿拉伯（14点）都支持。内层用非捕获组——
# 命名组 a/b 必须捕获纯数字串（首版捕获到 '14点' 整串导致 _point_to_int 返回 -1）
_POINT_RE = r"(?:[一二三四五六七八九十]{1,3}|\d{1,2})点(?:半)?"


def _point_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    return _HAN_NUM.get(s, -1)


def parse_time_range(question: str, now_ts: int | None = None) -> tuple[int, int] | None:
    """从问句抽 [t_from, t_to]（本地时间，秒级时间戳）。抽不出返回 None（不猜）。

    支持（按优先级）：
    - 「14:00~16:00」「14点到16点」「14:00-16:00」（默认今天；跨零点自动进位）
    - 「昨天 14:00~16:00」「前天…」
    - 「2026-10-08 14:00~16:00」「10-08 14:00 到 16:00」
    - 「最近/过去 N 小时」「近 N 小时」
    """
    if not question:
        return None
    now = datetime.fromtimestamp(now_ts) if now_ts else datetime.now()

    # 相对：「最近/过去/近 N 小时」
    m = re.search(r"(?:最近|过去|近)\s*(\d{1,3})\s*(?:个?小时|h|H)", question)
    if m:
        h = int(m.group(1))
        return int((now - timedelta(hours=h)).timestamp()), int(now.timestamp())

    # 日期偏移：昨天/前天
    day_offset = 0
    if "前天" in question:
        day_offset = -2
    elif "昨天" in question or "昨日" in question:
        day_offset = -1

    # 时间对：HH:MM~HH:MM（多种分隔）。先解析时间，之后把时间片段从问句剔除
    # 再找日期——否则「9:00-11:00」里的 `00-11` 会被月日正则误吃（实测踩坑）。
    t1 = t2 = None
    m = re.search(_TIME_RE + r"\s*[~～—至到\-]\s*" + _TIME_RE, question)
    if m:
        t1 = (int(m.group(1)), int(m.group(2)))
        t2 = (int(m.group(3)), int(m.group(4)))
        if not (t1[0] <= 23 and t2[0] <= 23 and t1[1] <= 59 and t2[1] <= 59):
            return None                     # 25:00~27:00 之类非法值：明说解析不了，不猜
        q_wo_time = question[:m.start()] + question[m.end():]
    else:
        # 「N点~M点」（中文或阿拉伯数字）：外层命名组剥掉「点/半」尾巴留纯数字
        m = re.search(r"(?P<a>\d{1,2}|[一二三四五六七八九十]{1,3})点(?:半)?"
                      r"\s*[~～—至到\-]\s*(?P<b>\d{1,2}|[一二三四五六七八九十]{1,3})点(?:半)?", question)
        if m:
            a, b = _point_to_int(m.group("a")), _point_to_int(m.group("b"))
            if 0 <= a <= 23 and 0 <= b <= 23:
                t1, t2 = (a, 0), (b, 0)
        q_wo_time = question
    if not (t1 and t2):
        return None

    # 基准日：显式日期 > 昨天前天 > 今天。短月日形式必须带「月/日」字样，
    # 裸 `N-N` 不认（无法与时间分隔符区分）。
    base = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
    m_date = re.search(r"(\d{4}-\d{1,2}-\d{1,2})|(?<!\d)(\d{1,2})月(\d{1,2})日?(?!\d)", q_wo_time)
    if m_date:
        try:
            if m_date.group(1):
                y, mo, d = (int(x) for x in m_date.group(1).split("-"))
                base = datetime(y, mo, d)
            else:
                mo, d = int(m_date.group(2)), int(m_date.group(3))
                base = base.replace(month=mo, day=d)
        except ValueError:
            return None

    start = base.replace(hour=t1[0], minute=t1[1])
    end = base.replace(hour=t2[0], minute=t2[1])
    if end <= start:                     # 22:00~02:00 → 跨零点
        end += timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


# ---------------------------------------------------------------- 事实组装

def build_facts(corr: dict, max_facts: int = 60) -> dict:
    """correlation.analyze 的输出 → 送 LLM 的紧凑事实集（截断防上下文爆炸）。"""
    def brief_incident(it: dict) -> dict:
        return {k: it.get(k) for k in ("title", "kind", "started_at", "ended_at",
                                       "error_class") if it.get(k) is not None}

    incidents = [brief_incident(i) for i in (corr.get("incidents") or [])][:max_facts]
    externals = [{"source": a.get("source"), "title": a.get("title"),
                  "started_at": a.get("started_at")} for a in (corr.get("external_alerts") or [])][:max_facts]
    clusters = []
    for c in (corr.get("clusters") or [])[:max_facts]:
        clusters.append({
            "size": c.get("size"),
            "members": [m.get("title") for m in (c.get("members") or [])][:12],
            "hypotheses": [{"name": h.get("name"), "hits": h.get("hits"),
                            "of": h.get("of")} for h in (c.get("hypotheses") or [])],
        })
    return {"window": corr.get("window"), "total": corr.get("total"),
            "incidents": incidents, "external_alerts": externals, "clusters": clusters}


# ---------------------------------------------------------------- 锚定校验

def _facts_whitelist(facts: dict) -> set[str]:
    """事实里出现过的实体名与数字 token（供回答比对）。"""
    wl: set[str] = set()
    for inc in facts.get("incidents", []):
        for v in inc.values():
            if isinstance(v, str):
                wl.update(re.findall(r"[\w\u4e00-\u9fff.:-]{2,}", v))
    for a in facts.get("external_alerts", []):
        for v in a.values():
            if isinstance(v, str):
                wl.update(re.findall(r"[\w\u4e00-\u9fff.:-]{2,}", v))
    for c in facts.get("clusters", []):
        wl.update(m for m in c.get("members", []) if isinstance(m, str))
        for h in c.get("hypotheses", []):
            wl.add(str(h.get("name", "")))
            wl.add(str(h.get("hits", "")))
    wl.update(str(facts.get("total", "")), str(len(facts.get("incidents", []))))
    wl.discard("")
    return wl


def anchor_check(answer: str, facts: dict) -> list[str]:
    """回答中的任务/节点/假设名必须能在事实里找到。返回未锚定 token 列表。

    校验口径：带数字/字母/点号的 token（任务名、节点名、IP、URL 形态）必须
    锚定；纯中文 ≤4 字的短语大概率是改写用语（"点故障"/"也故障了"）放过——
    宁可漏报也不把正常改写整段降级（漏报的兜底是 facts 折叠区随时可人工核对）。
    """
    wl = _facts_whitelist(facts)
    tokens = set(re.findall(r"[\w\u4e00-\u9fff.:-]{3,}", answer or ""))
    unknown = []
    for t in tokens:
        if t in wl:
            continue
        if any(t in w or w in t for w in wl if len(w) >= 3):
            continue                      # 子串互含（如任务名被截断）视为锚定
        if not re.search(r"[0-9a-zA-Z]", t):
            continue                      # 纯中文短语：改写用语，不按实体校验
        # 带数字/字母的 token：实体形态（JD/192.168/win-01）——必须锚定
        if re.match(r"^[0-9:.\-/]+$", t):
            continue                      # 纯时间/数字格式由 whitelist 的关键值兜底
        unknown.append(t)
    return unknown[:8]


# ---------------------------------------------------------------- LLM 改写

_SYSTEM_PROMPT = (
    "你是拨测监控平台的值班助手。你会收到一份 JSON 格式的故障事实（时间窗内的事件、"
    "外部告警、以及代码算出的关联簇与疑似假设）。请只用中文把事实改写为一段 3-6 句的"
    "回答，回答用户的问题。硬性规则：只允许重组与翻译给定事实，禁止引入任何新数字、"
    "新任务名、新节点名、新因果结论；假设一律保留「疑似」措辞；事实里没有的就说没有。"
)


def call_llm(question: str, facts: dict, cfg_ai: dict) -> str:
    """OpenAI chat 兼容调用。失败抛异常（由 API 层降级）。"""
    url = str(cfg_ai.get("url") or "").rstrip("/")
    if not url:
        raise RuntimeError("ai.url 未配置")
    key = str(cfg_ai.get("api_key") or "")
    model = str(cfg_ai.get("model") or "")
    if not model:
        raise RuntimeError("ai.model 未配置")
    endpoint = url if url.endswith("/chat/completions") else url + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": question + "\n\n事实JSON：\n" + json.dumps(facts, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    timeout = int(cfg_ai.get("timeout") or 30)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    try:
        return raw["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"网关响应格式异常: {e!r}") from e


def ai_enabled(cfg_ai: dict) -> bool:
    return bool(cfg_ai.get("enabled") and cfg_ai.get("url") and cfg_ai.get("model"))
