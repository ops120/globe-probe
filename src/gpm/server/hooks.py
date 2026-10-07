"""第三方告警接入（.docs/ONCALL_OPTIMIZATION_2.md 第六期 24/25/30）。

设计边界（写清楚，避免读者以为接得比实际多）：

- **只接收、不回写**：本模块不产生任何对外请求，第三方告警一律只读。双向同步的反馈环
  留到确有需求再说。
- **各家 payload 形态不同**：解析器只做「取字段」，**不猜语义**。取不到就置空并保留 raw，
  宁可在页面上显示不全，也不伪造一个看起来合理的值。文本型来源（钉钉/Teams/generic）
  的状态与严重度从文本关键词判，判不出按 firing 兜底（宁可多报一条 firing，
  不把故障装成已恢复）。
- **签名差异如实说明**：各家（含钉钉加签、Teams Bot Framework 鉴权）的「官方签名」机制
  并不统一（GCP Pub/Sub push 用 OIDC JWT、腾讯云有自己的签名算法、Grafana/Zabbix 侧通常
  靠自定义 header）。本期只实现**各家都做得到**的共享 Token（header 或 query），
  并把「未实现官方签名校验」明确列进限制；不假装已经验证了签名。
- **`raw_json` 落库前脱敏**：键名命中敏感词一律替换，URL 里的敏感查询参数与 userinfo 去掉
  （钉钉回调里的 sessionWebhook、Teams 的凭据类字段都会被这条规则抹掉）。
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import hmac
import json
import re
import time

from ..config import Config

SOURCES = ("grafana", "zabbix", "tencent", "gcp", "dingtalk", "teams", "generic")

# 接入 Token 的请求头与查询参数名（两家都能配：Grafana 有自定义 header，
# Zabbix webhook 是脚本、腾讯云/GCP 的 URL 由你自己给，塞 query 即可）
TOKEN_HEADER = "X-GPM-Hook-Token"
TOKEN_QUERY = "token"

# —— 以下阈值由 cfg.hook.* 提供；保留模块级同名常量供 api_web.py 直接读取。
# 默认值与 src/gpm/config.py DEFAULTS["hook"] 对齐；运行期 init(cfg) 会覆盖。
MAX_BODY_BYTES = 262144             # 单次 payload 上限（256 KB）
RATE_LIMIT_PER_MIN = 120            # 每来源每分钟接收上限（防被打爆）

_cfg: Config | None = None


def init(cfg) -> None:
    """由 app 在启动时注入 cfg；之后 MAX_BODY_BYTES / RATE_LIMIT_PER_MIN 同步到 cfg.hook.*。"""
    global _cfg, MAX_BODY_BYTES, RATE_LIMIT_PER_MIN
    _cfg = cfg
    try:
        MAX_BODY_BYTES = int(cfg.hook.get("max_body_bytes", MAX_BODY_BYTES) or MAX_BODY_BYTES)
    except Exception:
        pass
    try:
        RATE_LIMIT_PER_MIN = int(cfg.hook.get("rate_limit_per_min", RATE_LIMIT_PER_MIN) or RATE_LIMIT_PER_MIN)
    except Exception:
        pass

_SENSITIVE = ("token", "secret", "password", "passwd", "passphrase", "apikey", "api_key",
              "authorization", "auth", "sign", "signature", "credential", "private_key",
              "webhook", "cookie", "session")


# ---------------------------------------------------------------- 解析

def _first(d, *keys, default=""):
    """按顺序取第一个非空值（各家字段名大小写/下划线写法不一）。

    d 允许是 None / 非 dict（payload 千奇百怪），一律返回 default 而不是抛异常。
    """
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _ts_from_any(v) -> int:
    """把秒 / 毫秒 / RFC3339 字符串统一成秒。取不到返回 0（不猜当前时间）。"""
    if v in (None, ""):
        return 0
    if isinstance(v, (int, float)):
        n = int(v)
        return n // 1000 if n > 10 ** 11 else n       # 毫秒 → 秒
    s = str(v).strip()
    if re.fullmatch(r"\d+", s):
        return _ts_from_any(int(s))
    # RFC3339（Grafana 的 startsAt）——必须区分「带时区」与「不带时区」：
    # 带 Z / 偏移的按 UTC 解析，否则整条告警的时间会整体偏一个时区（实测差 8 小时）。
    if "T" in s or re.match(r"\d{4}-\d{2}-\d{2} ", s):
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
            if dt.tzinfo is None:
                return int(time.mktime(dt.timetuple()))    # 无时区 → 按本地时间理解
            return int(dt.timestamp())
        except (ValueError, OverflowError):
            return 0
    return 0


def _labels_of(d) -> dict:
    v = d.get("labels")
    return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}


def parse_grafana(body: dict) -> list[dict]:
    """Grafana Alertmanager webhook（标准 webhook contact point）。

    alerts[] 里每条一个告警；唯一 id 用 fingerprint（Alertmanager 提供）。
    """
    out = []
    common = _labels_of(body)
    for a in body.get("alerts") or []:
        if not isinstance(a, dict):
            continue
        labels = dict(common)
        labels.update(_labels_of(a))
        ann = a.get("annotations") if isinstance(a.get("annotations"), dict) else {}
        # 唯一 id：优先 Alertmanager 的 fingerprint；否则用 alertname|instance。
        # 注意**不能**写成 "" + "|" + "" —— 那会拼出 "|" 这个非空假 id，把没有
        # 任何标识的告警也收进来（测试抓到的）。
        sid = str(_first(a, "fingerprint") or "")
        if not sid:
            parts = [str(_first(labels, "alertname")), str(_first(labels, "instance"))]
            sid = "|".join(p for p in parts if p)
        if not sid:
            continue
        out.append({
            "source_id": str(sid),
            "title": str(_first(ann, "summary", default="") or _first(labels, "alertname")),
            "severity": str(_first(labels, "severity")),
            "status": "resolved" if str(a.get("status")) == "resolved" else "firing",
            "started_at": _ts_from_any(a.get("startsAt")),
            "ended_at": _ts_from_any(a.get("endsAt")),
            "labels": labels,
            "url": str(_first(a, "generatorURL", "panelURL", default="")
                       or _first(body, "externalURL")),
            "raw": a,
        })
    return out


def parse_zabbix(body: dict) -> list[dict]:
    """Zabbix 没有标准 webhook body（media type 是用户脚本），这里约定一个最小 JSON。

    约束写在这里，便于在 Zabbix 的 media type 脚本里照着拼：
      {"event_id","trigger","host","severity","status":"PROBLEM|OK","clock","url"}
    """
    sid = _first(body, "event_id", "eventid", "id")
    if not sid:
        return []
    st = str(_first(body, "status", "event_status")).upper()
    labels = {}
    for k in ("host", "host_name", "trigger", "namespace", "item"):
        v = _first(body, k)
        if v:
            labels[k] = str(v)
    return [{
        "source_id": str(sid),
        "title": str(_first(body, "trigger", "name", "subject")),
        "severity": str(_first(body, "severity", "priority")),
        "status": "resolved" if st in ("OK", "RESOLVED", "0") else "firing",
        "started_at": _ts_from_any(_first(body, "clock", "started_at")),
        "ended_at": _ts_from_any(body.get("ended_at")),
        "labels": labels,
        "url": str(_first(body, "url", "link")),
        "raw": body,
    }]


def parse_tencent(body: dict) -> list[dict]:
    """腾讯云云监控「告警回调」。字段名做容错，认不出就只保留 raw。"""
    sid = _first(body, "alarmId", "alarm_id", "id")
    if not sid:
        return []
    st = str(_first(body, "alarmStatus", "alarm_status", "status"))
    firing = st not in ("0", "OK", "RESOLVED", "resolved")
    inst = body.get("instanceObject") if isinstance(body.get("instanceObject"), dict) else {}
    labels = {}
    for k, v in (("namespace", _first(body, "namespace")),
                 ("metric", _first(body, "metricName", "metric_name")),
                 ("instance", _first(inst, "instanceName", "instanceId")
                  or _first(body, "instanceName", "instanceId")),
                 ("region", _first(body, "region"))):
        if v:
            labels[k] = str(v)
    return [{
        "source_id": str(sid),
        "title": str(_first(body, "alarmName", "alarm_name", "alarmType")),
        "severity": str(_first(body, "alarmLevel", "alarm_level", "severity")),
        "status": "firing" if firing else "resolved",
        "started_at": _ts_from_any(_first(body, "alarmTime", "alarm_time", "firstOccurTime")),
        "ended_at": _ts_from_any(body.get("recoverTime")),
        "labels": labels,
        "url": str(_first(body, "url", "detailUrl")),
        "raw": body,
    }]


def parse_gcp(body: dict) -> list[dict]:
    """GCP Cloud Monitoring → Pub/Sub push。

    push 的外层是 {"message": {"data": "<base64>", ...}}；也接受直接给 incident 对象
    （自建转发 / 测试用）。
    """
    inner = body
    msg = body.get("message")
    if isinstance(msg, dict) and msg.get("data"):
        try:
            inner = json.loads(base64.b64decode(str(msg["data"])).decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            inner = {}
    inc = inner.get("incident") if isinstance(inner.get("incident"), dict) else inner
    if not isinstance(inc, dict):
        return []
    sid = _first(inc, "incident_id", "incidentId", "id")
    if not sid:
        return []
    state = str(_first(inc, "state", default="open")).lower()
    labels = {}
    res = inc.get("resource") if isinstance(inc.get("resource"), dict) else {}
    met = inc.get("metric") if isinstance(inc.get("metric"), dict) else {}
    for k, v in (("policy", _first(inc, "policy_name", "policyName")),
                 ("resource_type", _first(res, "type")),
                 ("resource", _first(res, "name") or _first(inc, "resource_name")),
                 ("metric", _first(met, "type") or _first(inc, "metric_type")),
                 ("project", _first(inc, "project_id", "projectId"))):
        if v:
            labels[k] = str(v)
    return [{
        "source_id": str(sid),
        "title": str(_first(inc, "summary", "policy_name", "policyName")),
        "severity": str(_first(inc, "severity", default="")),
        "status": "resolved" if state in ("closed", "resolved") else "firing",
        "started_at": _ts_from_any(_first(inc, "started_at", "startTime")),
        "ended_at": _ts_from_any(_first(inc, "ended_at", "endTime")),
        "labels": labels,
        "url": str(_first(inc, "url", "incident_url")),
        "raw": inner,
    }]


# ---------------------------------------------------------------- 文本型告警（钉钉群 / Teams 频道 / 任意来源）

# 状态词表：从 IM 群消息文本里判 firing/resolved。命中恢复词 → resolved；
# 其余只要像告警就按 firing（不猜第三种状态——宁可多报一条 firing，也不把
# 故障装成已恢复）。恢复词必须排除「未/没/无/尚/不」类否定前缀——「故障未恢复」
# 「故障没有恢复」「无法恢复」「not recovered」都是 firing，由 _NOT_RECOVERED_RE 兜住。
_RESOLVED_RE = re.compile(
    r"(?<![未没无尚不])恢复|已修复|(?<![未没无尚不])解决|(?<![不未])正常\b|resolved|recovered|\bok\b",
    re.I)
_NOT_RECOVERED_RE = re.compile(
    r"(未|没|无|尚|不|无法|没有)[^，。,,.；;！!]{0,4}(恢复|修复|解决|正常)"
    r"|\bnot\s+(recovered|resolved|ok|fixed|restored)", re.I)
_SEVERITY_RE = re.compile(r"\bP[0-4]\b|critical|high|warning|warn|info|严重|紧急|警告|提示", re.I)
_MD_NOISE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[(?!\^)[^\]]*\]\([^)]*\)|[#*`>_]")
# 幂等指纹的截断词：第一个状态/处置词（含其常见的 已/未/请 前缀）之后的内容
# （请尽快处理/已恢复/...）不属于「什么故障」——firing 与恢复消息在此对齐
_TEXT_CUT_RE = re.compile(
    r"已|未|请|恢复|处理|排查|修复|解决|确认|关注|介入|resolved|recovered|fixed", re.I)


def _text_status(title: str, text: str) -> str:
    joined = " ".join((title, text))
    if _RESOLVED_RE.search(joined) and not _NOT_RECOVERED_RE.search(joined):
        return "resolved"
    return "firing"


def _text_severity(title: str, text: str) -> str:
    m = _SEVERITY_RE.search(" ".join((title, text)))
    return m.group(0) if m else ""


def _first_meaningful_line(text: str, limit: int = 120) -> str:
    """取第一条有内容的行并剥掉 markdown 噪声，作为标题兜底。"""
    for ln in str(text or "").splitlines():
        clean = _MD_NOISE_RE.sub("", ln).strip()
        if clean:
            return clean[:limit]
    return ""


def _stable_text_id(title: str, text: str) -> str:
    """文本告警的幂等 id：对「内容指纹」做哈希（启发式，见约束说明）。

    归一化三步：
      1. 抹掉每次消息都在变的元数据——URL、日期时间、epoch（只剥 16~19xx 开头的
         类 epoch 数，订单号这类业务单号**保留**：它们常常就是对象标识）；
      2. 在第一个「状态/处置」词处**截断**——firing 与恢复消息共享同一段「什么事
         +哪个对象」前缀，尾部（请尽快处理 / 已恢复）不是身份的一部分；
      3. 剩余文本整体小写哈希。
    已知取舍：截断词表覆盖不到的尾部差异（如两条不同故障共用同一前缀）会并成
    同 id——对 IM 文本告警这是可接受的启发式，结构化来源请自带 fingerprint。
    """
    norm = re.sub(r"https?://\S+", " ", str(title) + " " + str(text))
    norm = re.sub(r"【[^】]{0,12}】", " ", norm)     # 【P1】等严重度/频道前缀不算身份
    norm = re.sub(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}[:：]\d{2}(:\d{2})?", " ", norm)
    norm = re.sub(r"\b1[6-9]\d{8}\b|\b1[7-9]\d{11}\b", " ", norm)   # 类 epoch 秒/毫秒
    norm = re.sub(r"[\d.]+%|\b\d+(\.\d+)?\s*(ms|秒|分|分钟|小时)\b", " ", norm, flags=re.I)
    m = _TEXT_CUT_RE.search(norm)
    if m:
        norm = norm[:m.start()]
    norm = re.sub(r"\s+", " ", norm).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _text_alert_record(title: str, text: str, labels: dict, ts_hint=0) -> dict:
    title = str(title or "").strip() or _first_meaningful_line(text)
    labels = {str(k): str(v) for k, v in (labels or {}).items() if v not in (None, "")}
    return {
        "source_id": _stable_text_id(title, text),
        "title": title[:200],
        "severity": _text_severity(title, text),
        "status": _text_status(title, text),
        "started_at": _ts_from_any(ts_hint),
        "ended_at": 0,       # 文本告警没有结构化恢复时间：恢复走「同 id 后到覆盖」，不猜
        "labels": labels,
        "url": "",
        "raw": {"title": title, "text": str(text or "")[:2000], "labels": labels},
    }


def parse_dingtalk(body: dict) -> list[dict]:
    """钉钉群机器人回调（企业内部机器人 HTTP 回调 / Stream 推送把群消息转发给 gpm）。

    接收形态：{"msgtype":"text","text":{"content":...},"senderNick":...,"timestamp":...}
    与 {"msgtype":"markdown","markdown":{"title":...,"text":...}}。监控类告警到钉钉群
    的存量形态就是这两种机器人消息；本解析器只取字段不猜语义，状态/严重度从文本判。
    钉钉官方的加签/签名校验未实现（与四家同口径：共享 Token），见文档限制。
    """
    if not isinstance(body, dict):
        return []
    mt = str(_first(body, "msgtype", "msgType", default="text")).lower()
    labels = {}
    for k in ("senderNick", "senderStaffId", "conversationTitle", "conversationId"):
        v = _first(body, k)
        if v:
            labels["dingtalk_" + k] = str(v)
    if mt == "markdown":
        md = body.get("markdown")
        if not isinstance(md, dict):
            md = {}
        rec = _text_alert_record(str(md.get("title") or ""), str(md.get("text") or md.get("content") or ""),
                                 labels, ts_hint=_first(body, "timestamp", "createAt", "createTime"))
        return [rec] if rec["title"] else []
    txt = body.get("text")
    if not isinstance(txt, dict):
        txt = {}
    content = str(_first(txt, "content") or "")
    if not content.strip():
        # 钉钉 actionCard / 其它扩展形态：有 text 字段就吃，认不出如实留空（0 条 + raw 已在调用方落库口径之外）
        return []
    rec = _text_alert_record("", content, labels,
                             ts_hint=_first(body, "timestamp", "createAt", "createTime"))
    return [rec] if rec["title"] else []


def parse_teams(body: dict) -> list[dict]:
    """Microsoft Teams 告警消息获取。

    Teams 原生没有「把频道消息推给任意 webhook」的免费通道，工程上最省事的存量
    兼容法是 Teams Workflows(Power Automate)「发布频道消息时」触发器 → HTTP POST
    转发到 gpm。这里兼容三种到达形态（认不出就 0 条，不猜）：
      1. Office/Workflows MessageCard：{"@type":"MessageCard","title":...,"text":...}
      2. Bot Framework 消息：{"type":"message","attachments":[{contentType:
         "application/vnd.microsoft.card.adaptive","content":{body:[{type:"TextBlock",text:...}]}}]}
      3. 简单转发：{"title":...,"text":...} / {"text":...}
    与上游监控直连 webhook（Alertmanager/Grafana 格式，已支持）相比，这是给
    「告警只进 Teams 群」的存量链路兜底的通道。
    """
    if not isinstance(body, dict):
        return []
    labels = {}
    conv = body.get("conversation")
    if not isinstance(conv, dict):
        conv = {}
    if conv.get("name"):
        labels["teams_channel"] = str(conv["name"])
    sender = body.get("from")
    if isinstance(sender, dict) and isinstance(sender.get("user"), dict) \
            and sender["user"].get("displayName"):
        labels["teams_sender"] = str(sender["user"]["displayName"])
    texts: list[str] = []
    title = str(_first(body, "title", "summary", default="") or "")
    if title:
        texts.append(title)
    if body.get("text"):
        texts.append(str(body["text"]))
    card = body.get("attachments")
    if not isinstance(card, list):
        card = []
    for att in card:
        c = att.get("content") if isinstance(att, dict) else None
        if not isinstance(c, dict):
            continue
        if c.get("title"):
            texts.append(str(c["title"]))
        if c.get("text"):
            texts.append(str(c["text"]))
        blocks = c.get("body")
        if not isinstance(blocks, list):
            blocks = []
        for b in blocks:
            if isinstance(b, dict) and b.get("text"):
                texts.append(str(b["text"]))
    if not texts:
        return []
    rec = _text_alert_record(title, "\n".join(texts), labels,
                             ts_hint=_first(body, "timestamp", "createdDateTime"))
    return [rec] if rec["title"] else []


def parse_generic(body: dict) -> list[dict]:
    """通用告警来源：给「自有系统 / 没有现成适配器」的最后一个兜底入口。

    优先吃结构化字段（title/status/severity/startsAt/labels/fingerprint），取不到
    退化为文本解析。设计动机：与其让每个自有系统各自想办法挤进 IM 群再让人肉搬运，
    不如直接 POST 到 gpm——IM 群适合人看，聚合判断留给平台。
    """
    if not isinstance(body, dict):
        return []
    # source_id 是幂等主键：上游给多长都截到 128，防畸形 payload 把主键撑爆
    sid = str(_first(body, "fingerprint", "source_id", "id", "event_id", default="") or "")[:128]
    title = str(_first(body, "title", "summary", "name", default="") or "")
    text = str(_first(body, "text", "message", "description", default="") or "")
    labels = body.get("labels")
    if not isinstance(labels, dict):
        labels = {}
    labels = dict(labels)
    for k in ("host", "instance", "service", "env", "region"):
        v = _first(body, k)
        if v and k not in labels:
            labels[k] = str(v)
    st = str(_first(body, "status", "state", default="")).lower()
    if not sid:
        sid = _stable_text_id(title or text, text)
    if not title and not text:
        return []
    return [{
        "source_id": sid,
        "title": (title or _first_meaningful_line(text))[:200],
        "severity": str(_first(body, "severity", "priority", default="") or _text_severity(title, text)),
        "status": "resolved" if st in ("resolved", "recovered", "ok", "closed", "0") else "firing",
        "started_at": _ts_from_any(_first(body, "startsAt", "started_at", "starts_at", "time", "ts")),
        "ended_at": _ts_from_any(_first(body, "endsAt", "ended_at", "ends_at")),
        "labels": {str(k): str(v) for k, v in labels.items() if v not in (None, "")},
        "url": str(_first(body, "url", "link", "generatorURL", default="") or ""),
        "raw": body,
    }]


PARSERS = {"grafana": parse_grafana, "zabbix": parse_zabbix,
           "tencent": parse_tencent, "gcp": parse_gcp,
           "dingtalk": parse_dingtalk, "teams": parse_teams, "generic": parse_generic}


def parse(source: str, body: dict) -> list[dict]:
    """按来源解析；未知来源返回空列表（调用方按 0 条处理并记审计）。"""
    fn = PARSERS.get(source)
    return fn(body) if fn and isinstance(body, dict) else []


# ---------------------------------------------------------------- 脱敏

_URL_SENSITIVE = re.compile(
    r"([?&](?:" + "|".join(_SENSITIVE) + r")=)[^&#\s]*", re.I)


def _redact_str(s: str) -> str:
    """URL / 文本里的敏感查询参数与 userinfo 一律抹掉。"""
    out = _URL_SENSITIVE.sub(r"\1***", s)
    # http://user:pass@host/... → http://***@host/...
    out = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1***@", out, flags=re.I)
    return out


def redact(obj, depth: int = 0):
    """递归脱敏。命中敏感键名 → 整值替换；字符串做 URL/凭据清洗。

    depth 上限防止畸形 payload 把栈打爆；非 JSON 标量原样返回。
    """
    if depth > 8:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            ks = str(k).lower()
            if any(t in ks for t in _SENSITIVE):
                out[str(k)] = "***"
            else:
                out[str(k)] = redact(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(x, depth + 1) for x in obj[:200]]
    if isinstance(obj, str):
        return _redact_str(obj)[:2000]
    return obj


# ---------------------------------------------------------------- 鉴权 / 限流

def expected_token(storage, source: str) -> str:
    """该来源应使用的接入 Token：优先 `hook_token_<source>`，其次通用 `hook_token`。

    两者都为空 = 未配置 → 拒绝接收（不提供「无鉴权也能写」的默认）。
    """
    get = getattr(storage, "setting_get", None)
    if not callable(get):
        return ""
    per = str(get("hook_token_%s" % source, "") or "").strip()
    return per or str(get("hook_token", "") or "").strip()


def token_ok(storage, source: str, header_token: "str | None",
             query_token: "str | None") -> bool:
    """常量时间比较，避免通过响应时间探测 Token。"""
    want = expected_token(storage, source)
    if not want:
        return False
    got = (header_token or query_token or "").strip()
    return bool(got) and hmac.compare_digest(want, got)


_RATE: dict = {}          # source -> (window_start, count)


def rate_ok(source: str, ts: int | None = None, limit: int | None = None) -> bool:
    """每来源每分钟的简单窗口限流（进程内；重启即清零，够用作防打爆）。

    limit 留空时取模块级 RATE_LIMIT_PER_MIN（由 init(cfg) 同步为 cfg.hook.rate_limit_per_min）。
    """
    if limit is None:
        limit = RATE_LIMIT_PER_MIN
    now_s = int(ts or time.time())
    win = now_s // 60
    cur = _RATE.get(source)
    if not cur or cur[0] != win:
        _RATE[source] = (win, 1)
        return True
    if cur[1] >= limit:
        return False
    _RATE[source] = (win, cur[1] + 1)
    return True


def rate_reset() -> None:
    """测试辅助：清空限流账本。"""
    _RATE.clear()


# ---------------------------------------------------------------- 签名校验（预留扩展点，2026-10-06 决议 #5）

# 各家「官方签名」机制并不统一（钉钉机器人回调加签、Teams Bot Framework 鉴权、
# GCP OIDC JWT、腾讯云签名算法），且需要各家凭据/授权才能实现与联调。
# 决议：**先预留功能及 API**——这里给出活的扩展点（不是死配置键）：
#
#   1. 实现方注册 {source: verifier}，verifier 签名 fn(headers, body) -> (bool, 说明)；
#      注册即生效——接收端在 Token 与限流之后调用，False 一律 401。
#   2. 未注册的来源返回 (None, ...)，接收端按现状放行——共享 Token（token_ok，
#      常量时间比较）在扩展点之外仍是硬门槛，不存在「无鉴权」通道。
#   3. headers 约定传大小写不敏感映射（Starlette Headers 原样透传），verifier 内
#      用小写键名读取；签名密钥约定落 setting `hook_sign_<source>`（与 token 同一
#      读写路径），由具体 verifier 自行读取——本期不预建该键，避免无消费方的死配置。
SIGNATURE_VERIFIERS: dict = {}


def register_signature_verifier(source: str, fn) -> None:
    """注册某来源的签名校验器（预留 API；测试可临时注册后注销）。"""
    SIGNATURE_VERIFIERS[source] = fn


def validate_signature(source: str, headers, body) -> tuple:
    """调用来源签名校验器（若已注册）。返回 (ok, 说明)：

    ok=True 通过；ok=False 失败（接收端 401）；ok=None 未注册（共享 Token 模式放行）。
    校验器自身异常一律转 False——签名环节宁拒勿放。"""
    fn = SIGNATURE_VERIFIERS.get(source)
    if fn is None:
        return None, "该来源未注册签名校验器（当前为共享 Token 模式）"
    try:
        ok, msg = fn(headers, body)
        return bool(ok), str(msg or "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def signature_modes() -> dict:
    """各来源当前鉴权模式（/api/external/settings 透出，供前端/运维核对）。"""
    return {src: ("signature+token" if src in SIGNATURE_VERIFIERS else "token")
            for src in SOURCES}
