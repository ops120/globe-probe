"""第三方告警接入（.docs/ONCALL_OPTIMIZATION_2.md 第六期 24/25/30）。

设计边界（写清楚，避免读者以为接得比实际多）：

- **只接收、不回写**：本模块不产生任何对外请求，第三方告警一律只读。双向同步的反馈环
  留到确有需求再说。
- **各家 payload 形态不同**：解析器只做「取字段」，**不猜语义**。取不到就置空并保留 raw，
  宁可在页面上显示不全，也不伪造一个看起来合理的值。
- **签名差异如实说明**：四家的「官方签名」机制并不统一（GCP Pub/Sub push 用 OIDC JWT、
  腾讯云有自己的签名算法、Grafana/Zabbix 侧通常靠自定义 header）。本期只实现**它家都
  做得到**的共享 Token（header 或 query），并把「未实现官方签名校验」明确列进限制；
  不假装已经验证了签名。
- **`raw_json` 落库前脱敏**：键名命中敏感词一律替换，URL 里的敏感查询参数与 userinfo 去掉。
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hmac
import json
import re
import time

SOURCES = ("grafana", "zabbix", "tencent", "gcp")

# 接入 Token 的请求头与查询参数名（两家都能配：Grafana 有自定义 header，
# Zabbix webhook 是脚本、腾讯云/GCP 的 URL 由你自己给，塞 query 即可）
TOKEN_HEADER = "X-GPM-Hook-Token"
TOKEN_QUERY = "token"

MAX_BODY_BYTES = 256 * 1024          # 单次 payload 上限
RATE_LIMIT_PER_MIN = 120             # 每来源每分钟接收上限（防被打爆）

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


PARSERS = {"grafana": parse_grafana, "zabbix": parse_zabbix,
           "tencent": parse_tencent, "gcp": parse_gcp}


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


def rate_ok(source: str, ts: int | None = None, limit: int = RATE_LIMIT_PER_MIN) -> bool:
    """每来源每分钟的简单窗口限流（进程内；重启即清零，够用作防打爆）。"""
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
