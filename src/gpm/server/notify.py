"""告警通知渠道：校验、描述、载荷构造与发送（仅标准库）。

支持 5 种渠道：
  webhook   通用 POST（format=json 默认 / text）
  wecom     企业微信机器人
  dingtalk  钉钉机器人（可选加签；仅支持 secret 加签方式）
  feishu    飞书机器人
  smtp      SMTP 邮件（ssl 为真时忽略 starttls）

对外接口（签名冻结）：
  validate(channel) -> str | None          非法配置返回中文错误说明，不抛异常
  describe(channel) -> str                 人类可读短标签，非法配置也不抛异常
  render_payload(channel, title, text)     -> dict | str（send 内部也用它）
  render_markdown(title, text) -> str      纯文本 → markdown 变体（供单测/复用）
  send(channel, title, text, timeout=8.0)  -> tuple[bool, str]  吞掉所有异常

markdown 渲染分支（ONCALL_OPTIMIZATION 第一期）：企微/钉钉/飞书三类 IM 渠道发送
markdown 变体（段落标签【X】加粗、裸链接转可点），webhook/smtp 保持纯文本：
  - wecom / dingtalk：msgtype=markdown（与历史一致），内容经 render_markdown 渲染；
  - feishu：官方自定义机器人**没有** msg_type=markdown（仅 text/post/interactive），
    故默认发 interactive 卡片（header=标题 + markdown 元素）；渠道配置
    {"markdown": false} 可整体回退纯文本（三类 IM 均生效，msgtype=text）。
统一约定：不打印日志（由调用方决定）；HTTP 超时统一走 timeout 参数；
HTTP 失败摘要为 "HTTP <状态码>: <响应体前 200 字符>"；
网络异常摘要为 "<异常类名>: <消息前 120 字符>"。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from ..config import Config

CHANNEL_TYPES = ("webhook", "wecom", "dingtalk", "feishu", "smtp")

# —— 以下阈值由 cfg.alert.* 提供；保留模块级同名常量供旧调用按属性名直接读取。
# 默认值与 src/gpm/config.py DEFAULTS["alert"] 对齐；运行期 init(cfg) 会覆盖。
DEFAULT_TIMEOUT = 8.0
DEFAULT_SMTP_PORT = 587
_BODY_LIMIT = 200
_ERR_LIMIT = 120
_USER_AGENT = "gpm-notify/1.0"
_JSON_CT = "application/json; charset=utf-8"
_TEXT_CT = "text/plain; charset=utf-8"

_cfg: Config | None = None


def init(cfg) -> None:
    """由 app 在启动时注入 cfg；同步 DEFAULT_TIMEOUT / DEFAULT_SMTP_PORT 到 cfg.alert.*。"""
    global _cfg, DEFAULT_TIMEOUT, DEFAULT_SMTP_PORT
    _cfg = cfg
    try:
        DEFAULT_TIMEOUT = float(cfg.alert.get("notify_default_timeout_seconds", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
    except Exception:
        pass
    try:
        DEFAULT_SMTP_PORT = int(cfg.alert.get("notify_default_smtp_port", DEFAULT_SMTP_PORT) or DEFAULT_SMTP_PORT)
    except Exception:
        pass

# markdown 变体：整行就是一个 http(s) 链接 → 转可点链接
_MD_URL_RE = re.compile(r"^https?://\S+$")
# markdown 变体：行首「【标签】」→ 加粗（配合 alerting 的固定段落）
_MD_LABEL_RE = re.compile(r"^(【[^】]{1,32}】)")


# ------------------------------------------------------------------ 小工具

def _s(v) -> str:
    return "" if v is None else str(v)


def _head(s, limit: int = _BODY_LIMIT) -> str:
    return _s(s).strip()[:limit]


def _as_int(v, default=None):
    if v is None:
        return default
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "on", "y"):
        return True
    if t in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _type_of(channel) -> str:
    if not isinstance(channel, dict):
        return ""
    return _s(channel.get("type")).strip().lower()


def _check_url(url, field: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return f"缺少 {field}（需要 http(s) 地址）"
    p = urllib.parse.urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.netloc:
        return f"{field} 不是合法的 http(s) 地址: {_head(url, 80)}"
    return None


def _recipients(channel) -> list:
    to = channel.get("mail_to")
    if isinstance(to, str):
        to = [to]
    if not isinstance(to, (list, tuple)):
        return []
    return [str(x).strip() for x in to if str(x or "").strip()]


# ------------------------------------------------------------------ validate

def validate(channel: dict) -> str | None:
    """校验渠道配置；合法返回 None，非法返回中文错误说明（不抛异常）。"""
    try:
        if not isinstance(channel, dict):
            return "渠道配置必须是字典"
        t = _type_of(channel)
        if not t:
            return "缺少 type 字段"
        if t not in CHANNEL_TYPES:
            return f"不支持的渠道类型: {t}（仅支持 {'/'.join(CHANNEL_TYPES)}）"

        if t == "webhook":
            err = _check_url(channel.get("url"), "url")
            if err:
                return err
            method = _s(channel.get("method") or "POST").strip().upper()
            if method not in ("POST", "PUT", "PATCH"):
                return f"method 仅支持 POST/PUT/PATCH，收到: {method}"
            fmt = _s(channel.get("format") or "json").strip().lower()
            if fmt not in ("json", "text"):
                return f"format 仅支持 json/text，收到: {fmt}"
            headers = channel.get("headers")
            if headers is not None and not isinstance(headers, dict):
                return "headers 必须是对象（键值对）"
            return None

        if t in ("wecom", "dingtalk", "feishu"):
            err = _check_url(channel.get("webhook"), "webhook")
            if err:
                return err
            if t == "dingtalk":
                secret = channel.get("secret")
                if secret is not None and not isinstance(secret, str):
                    return "secret 必须是字符串"
            return None

        # smtp
        host = channel.get("host")
        if not isinstance(host, str) or not host.strip():
            return "SMTP 缺少 host（服务器地址）"
        raw_port = channel.get("port")
        port = DEFAULT_SMTP_PORT if raw_port is None else _as_int(raw_port, None)
        if port is None or not (1 <= port <= 65535):
            return f"SMTP port 非法: {raw_port!r}（应为 1-65535 的整数）"
        mail_from = channel.get("mail_from")
        if not isinstance(mail_from, str) or "@" not in mail_from:
            return "SMTP 缺少合法的 mail_from（发件人地址）"
        rcpts = _recipients(channel)
        if not rcpts:
            return "SMTP 缺少 mail_to（收件人，至少 1 个）"
        for addr in rcpts:
            if "@" not in addr:
                return f"SMTP mail_to 收件人地址非法: {_head(addr, 80)}"
        return None
    except Exception as e:  # 校验绝不抛异常
        return f"配置校验异常: {type(e).__name__}: {_head(e, _ERR_LIMIT)}"


# ------------------------------------------------------------------ describe

def describe(channel: dict) -> str:
    """给人看的短标签；非法配置也不抛异常。"""
    try:
        t = _type_of(channel)
        if t == "webhook":
            url = channel.get("url") if isinstance(channel, dict) else None
            return f"Webhook {url}" if url else "Webhook (缺少 url)"
        if t == "wecom":
            return "企业微信机器人" if isinstance(channel, dict) and channel.get("webhook") \
                else "企业微信机器人 (缺少 webhook)"
        if t == "dingtalk":
            return "钉钉机器人" if isinstance(channel, dict) and channel.get("webhook") \
                else "钉钉机器人 (缺少 webhook)"
        if t == "feishu":
            return "飞书机器人" if isinstance(channel, dict) and channel.get("webhook") \
                else "飞书机器人 (缺少 webhook)"
        if t == "smtp":
            if not isinstance(channel, dict):
                return "SMTP (配置不完整)"
            mail_from = _s(channel.get("mail_from")).strip()
            rcpts = _recipients(channel)
            if mail_from and rcpts:
                return f"SMTP {mail_from} → {','.join(rcpts)}"
            return "SMTP (配置不完整)"
        if not t:
            return "未知渠道 (缺少 type)" if isinstance(channel, dict) else "未知渠道 (配置不是字典)"
        return f"未知渠道 {t}"
    except Exception:
        return "未知渠道 (配置异常)"


# ------------------------------------------------------------------ 加签/URL

def _dingtalk_sign(secret: str, ts_ms=None) -> tuple:
    """钉钉官方加签：timestamp + "\n" + secret 做 HMAC-SHA256，再 base64 + urlencode。"""
    ts = str(int(ts_ms if ts_ms is not None else time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    return ts, sign


def render_url(channel: dict) -> str:
    """实际请求的 URL（钉钉配了 secret 时附带 timestamp/sign 查询参数）。"""
    t = _type_of(channel)
    if t == "webhook":
        return _s(channel.get("url")).strip()
    if t in ("wecom", "dingtalk", "feishu"):
        url = _s(channel.get("webhook")).strip()
        if t == "dingtalk" and url:
            secret = _s(channel.get("secret")).strip()
            if secret:
                ts, sign = _dingtalk_sign(secret)
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}timestamp={ts}&sign={sign}"
        return url
    return ""


# ------------------------------------------------------------------ 载荷

def _md_body(text: str) -> str:
    """纯文本 → markdown 正文：段落标签加粗、（标签后的）裸链接转可点，其余原样保留。"""
    out = []
    for line in _s(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if _MD_URL_RE.match(stripped):
            out.append(f"[点击查看]({stripped})")
            continue
        m = _MD_LABEL_RE.match(line)
        if m:
            label, rest = m.group(1), line[m.end():]
            rest_s = rest.strip()
            if _MD_URL_RE.match(rest_s):
                out.append(f"**{label}** [点击查看]({rest_s})")
                continue
            out.append(f"**{label}**{rest}")
            continue
        out.append(line)
    return "\n".join(out)


def render_markdown(title: str, text: str) -> str:
    """(title, text) → markdown 变体：标题作一级标题，正文按段落渲染。"""
    return f"### {_s(title).strip()}\n{_md_body(text)}"


def render_payload(channel: dict, title: str, text: str):
    """公开的载荷构造函数（便于单测断言各平台报文体）。

    - webhook json: {"title","text","source":"gpm","ts"}；text: "title\\ntext"
    - wecom:   {"msgtype":"markdown","markdown":{"content":"### title\\nmd正文"}}
              （md正文 = 段落标签加粗、链接可点；渠道配置 markdown=false → msgtype=text）
    - dingtalk:{"msgtype":"markdown","markdown":{"title","text"}}；
              配了 secret 时额外回传 timestamp/sign（官方要求走 URL，
              send() 发送前会剥离这两个字段）；markdown=false → msgtype=text
    - feishu:  默认 {"msg_type":"interactive","card":{header + markdown 元素}}
              （官方自定义机器人没有 msg_type=markdown）；markdown=false →
              {"msg_type":"text","content":{"text":"title\\ntext"}}
    - smtp:    邮件要素摘要 {"subject","body","mail_from","mail_to"}（纯文本）
    """
    t = _type_of(channel)
    channel = channel if isinstance(channel, dict) else {}
    title_s, text_s = _s(title), _s(text)

    if t == "webhook":
        if _s(channel.get("format") or "json").strip().lower() == "text":
            return f"{title_s}\n{text_s}"
        return {"title": title_s, "text": text_s, "source": "gpm", "ts": int(time.time())}

    if t == "wecom":
        if _as_bool(channel.get("markdown"), True):
            return {"msgtype": "markdown",
                    "markdown": {"content": render_markdown(title_s, text_s)}}
        return {"msgtype": "text", "text": {"content": f"{title_s}\n{text_s}"}}

    if t == "dingtalk":
        if _as_bool(channel.get("markdown"), True):
            payload = {"msgtype": "markdown",
                       "markdown": {"title": title_s, "text": _md_body(text_s)}}
        else:
            payload = {"msgtype": "text", "text": {"content": f"{title_s}\n{text_s}"}}
        secret = _s(channel.get("secret")).strip()
        if secret:
            ts, sign = _dingtalk_sign(secret)
            payload["timestamp"] = ts
            payload["sign"] = sign
        return payload

    if t == "feishu":
        if _as_bool(channel.get("markdown"), True):
            return {
                "msg_type": "interactive",
                "card": {
                    "config": {"update_multi": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": title_s},
                        "template": "blue",
                    },
                    "elements": [{"tag": "markdown", "content": _md_body(text_s)}],
                },
            }
        return {"msg_type": "text", "content": {"text": f"{title_s}\n{text_s}"}}

    if t == "smtp":
        return {
            "subject": title_s,
            "body": text_s,
            "mail_from": _s(channel.get("mail_from")),
            "mail_to": _recipients(channel),
        }

    raise ValueError(f"不支持的渠道类型: {t or '(空)'}")


# ------------------------------------------------------------------ HTTP

def _read_http_error(e) -> str:
    try:
        raw = e.read()
    except Exception:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return _s(raw)


def _resp_status(resp) -> int:
    raw = getattr(resp, "status", None)
    if raw is None:
        raw = getattr(resp, "code", None)
    if raw is None:
        getcode = getattr(resp, "getcode", None)
        if callable(getcode):
            try:
                raw = getcode()
            except Exception:
                raw = None
    n = _as_int(raw)
    return 200 if n is None else n


def _close_quiet(resp) -> None:
    fn = getattr(resp, "close", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _http(url: str, data, timeout: float, headers=None, method: str = "POST"):
    """发一个 HTTP 请求。

    返回 (transport_ok, status, body_or_error)：
      True  -> status 为状态码，body_or_error 为响应体文本
      False -> status 为 0 或 HTTP 状态码，body_or_error 为错误摘要
    """
    hdrs = {"User-Agent": _USER_AGENT}
    hdrs.update({str(k): _s(v) for k, v in (headers or {}).items()})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        code = _as_int(getattr(e, "code", None), 0)
        return False, code, f"HTTP {code}: {_head(_read_http_error(e))}"
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {_head(e, _ERR_LIMIT)}"
    try:
        status = _resp_status(resp)
        raw = resp.read()
        body = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else _s(raw)
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {_head(e, _ERR_LIMIT)}"
    finally:
        _close_quiet(resp)
    return True, status, body


def _post_json(url: str, payload, timeout: float, method: str = "POST"):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _http(url, body, timeout, {"Content-Type": _JSON_CT}, method)


# ------------------------------------------------------------------ send

def _send_webhook(channel, title, text, timeout):
    payload = render_payload(channel, title, text)
    if isinstance(payload, str):
        data = payload.encode("utf-8")
        ctype = _TEXT_CT
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ctype = _JSON_CT
    headers = {"Content-Type": ctype}
    extra = channel.get("headers")
    if isinstance(extra, dict):
        headers.update({str(k): _s(v) for k, v in extra.items()})
    method = _s(channel.get("method") or "POST").strip().upper()
    ok, status, info = _http(render_url(channel), data, timeout, headers, method)
    if not ok:
        return False, info
    return True, f"HTTP {status}"


def _check_im_response(kind: str, status: int, body: str):
    try:
        data = json.loads(body)
    except Exception:
        return False, f"HTTP {status} 响应非 JSON: {_head(body)}"
    if not isinstance(data, dict):
        return False, f"HTTP {status} 响应格式异常: {_head(body)}"
    key = "code" if kind == "feishu" else "errcode"
    val = data.get(key)
    if val is None and kind == "feishu":
        val = data.get("StatusCode")
        key = "StatusCode"
    if val is None:
        return False, f"HTTP {status} 响应缺少 {key}: {_head(body)}"
    n = _as_int(val)
    if n is None:
        return False, f"HTTP {status} {key}={val!r} 无法解析: {_head(body)}"
    if n != 0:
        msg = data.get("errmsg") or data.get("msg") or data.get("StatusMessage") or ""
        return False, f"HTTP {status} {key}={n} {_head(msg)}".rstrip()
    return True, f"HTTP {status} {key}=0"


def _send_im(channel, title, text, timeout):
    kind = _type_of(channel)
    payload = render_payload(channel, title, text)
    if kind == "dingtalk" and isinstance(payload, dict):
        # 签名通过 URL 传递，报文体保持官方原样
        payload = {k: v for k, v in payload.items() if k not in ("timestamp", "sign")}
    ok, status, info = _post_json(render_url(channel), payload, timeout)
    if not ok:
        return False, info
    return _check_im_response(kind, status, info)


def _smtp_message(channel, title, text) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = _s(title)
    msg["From"] = _s(channel.get("mail_from"))
    msg["To"] = ", ".join(_recipients(channel))
    msg.set_content(_s(text))
    return msg


def _send_smtp(channel, title, text, timeout):
    host = _s(channel.get("host")).strip()
    port = _as_int(channel.get("port"), DEFAULT_SMTP_PORT)
    use_ssl = _as_bool(channel.get("ssl"), False)
    use_starttls = (not use_ssl) and _as_bool(channel.get("starttls"), True)
    user = _s(channel.get("user"))
    password = _s(channel.get("password"))
    rcpts = _recipients(channel)
    msg = _smtp_message(channel, title, text)

    cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    server = None
    try:
        server = cls(host, port, timeout=timeout)
        if use_starttls:
            server.starttls()
        if user:
            server.login(user, password)
        server.send_message(msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {_head(e, _ERR_LIMIT)}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return True, f"SMTP 已发送至 {', '.join(rcpts)}"


def send(channel: dict, title: str, text: str, timeout: float | None = None) -> tuple:
    """发送一条通知，返回 (是否成功, 说明/错误摘要)；任何异常都被吞掉转成 (False, 摘要)。

    timeout=None 表示用 DEFAULT_TIMEOUT——必须运行时读取：init() 会按
    alert.notify_default_timeout_seconds 重绑模块级默认值，若写成
    `timeout: float = DEFAULT_TIMEOUT`，默认参在函数定义期就绑死了，配置旋钮失效
    （「配了没用」的死旋钮，实测踩坑）。"""
    try:
        err = validate(channel)
        if err:
            return False, f"渠道配置非法: {err}"
        try:
            tmo = float(DEFAULT_TIMEOUT if timeout is None else timeout)
        except (TypeError, ValueError):
            return False, f"timeout 非法: {timeout!r}"
        if tmo <= 0:
            tmo = DEFAULT_TIMEOUT
        t = _type_of(channel)
        if t == "smtp":
            return _send_smtp(channel, title, text, tmo)
        if t == "webhook":
            return _send_webhook(channel, title, text, tmo)
        return _send_im(channel, title, text, tmo)
    except Exception as e:  # 兜底：绝不向调用方抛异常
        return False, f"{type(e).__name__}: {_head(e, _ERR_LIMIT)}"
