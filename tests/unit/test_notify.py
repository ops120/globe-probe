"""通知模块单测：5 种渠道的校验/描述/载荷，以及 send() 的 HTTP / SMTP 分支。

完全不依赖外网与数据库：HTTP 用 monkeypatch 替换 urllib.request.urlopen，
SMTP 用 monkeypatch 替换 smtplib.SMTP / SMTP_SSL 的假实现。
"""
import base64
import hashlib
import hmac
import io
import json
import smtplib
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server import notify  # noqa: E402


# ---------------------------------------------------------------- 测试样本

WEBHOOK = {"type": "webhook", "url": "https://example.com/hook"}
WEBHOOK_TEXT = {"type": "webhook", "url": "https://example.com/hook", "format": "text"}
WECOM = {"type": "wecom",
         "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"}
DINGTALK = {"type": "dingtalk",
            "webhook": "https://oapi.dingtalk.com/robot/send?access_token=tok"}
DINGTALK_SIGNED = dict(DINGTALK, secret="SECret123")
FEISHU = {"type": "feishu", "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"}
SMTP = {"type": "smtp", "host": "smtp.example.com", "port": 587, "user": "u",
        "password": "p", "mail_from": "a@b.com", "mail_to": ["c@d.com"]}
ALL_OK = [WEBHOOK, WECOM, DINGTALK, FEISHU, SMTP]


# ---------------------------------------------------------------- HTTP 替身

class _Resp:
    def __init__(self, body: bytes, status: int):
        self._body = body
        self.status = status
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


class FakeHTTP:
    """替换 urllib.request.urlopen：按队列依次返回响应或抛出异常。"""

    def __init__(self):
        self.calls = []
        self._queue = []

    def reply(self, body=b"", status=200):
        self._queue.append(("resp", body, status))
        return self

    def fail(self, exc):
        self._queue.append(("raise", exc, None))
        return self

    def __call__(self, req, timeout=None):
        self.calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "data": req.data,
            "headers": dict(req.headers),
            "timeout": timeout,
        })
        assert self._queue, "urlopen 被调用的次数超出预期"
        kind, a, b = self._queue.pop(0)
        if kind == "raise":
            raise a
        return _Resp(a, b)

    def install(self, monkeypatch):
        monkeypatch.setattr(urllib.request, "urlopen", self)
        return self

    def json_body(self, idx=0):
        return json.loads(self.calls[idx]["data"].decode("utf-8"))


@pytest.fixture
def http(monkeypatch):
    return FakeHTTP().install(monkeypatch)


def _http_error(url, code, body: bytes):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


# ---------------------------------------------------------------- SMTP 替身

class FakeSMTP:
    instances = []
    fail_at = None  # None / "connect" / "starttls" / "login" / "send"

    def __init__(self, host=None, port=None, timeout=None, **kw):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.messages = []
        self.__class__.instances.append(self)
        if self.__class__.fail_at == "connect":
            raise OSError("connection refused")

    def starttls(self):
        self.calls.append(("starttls",))
        if self.__class__.fail_at == "starttls":
            raise smtplib.SMTPException("tls handshake failed")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.__class__.fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    def send_message(self, msg):
        self.calls.append(("send_message",))
        self.messages.append(msg)
        if self.__class__.fail_at == "send":
            raise smtplib.SMTPRecipientsRefused({"c@d.com": (550, b"no such user")})

    def quit(self):
        self.calls.append(("quit",))


class FakeSMTPSSL(FakeSMTP):
    instances = []


@pytest.fixture
def smtp_fakes(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTPSSL.instances = []
    FakeSMTP.fail_at = None
    FakeSMTPSSL.fail_at = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    yield FakeSMTP
    FakeSMTP.fail_at = None
    FakeSMTPSSL.fail_at = None


# ---------------------------------------------------------------- validate

@pytest.mark.parametrize("channel", ALL_OK, ids=[c["type"] for c in ALL_OK])
def test_validate_legal_returns_none(channel):
    assert notify.validate(channel) is None


@pytest.mark.parametrize("channel, keyword", [
    # webhook
    ({"type": "webhook"}, "url"),
    ({"type": "webhook", "url": ""}, "url"),
    ({"type": "webhook", "url": "example.com/hook"}, "http"),
    ({"type": "webhook", "url": "ftp://x/y"}, "http"),
    (dict(WEBHOOK, format="xml"), "format"),
    (dict(WEBHOOK, method="DELETE"), "method"),
    (dict(WEBHOOK, headers=["a"]), "headers"),
    # 企业微信 / 钉钉 / 飞书
    ({"type": "wecom"}, "webhook"),
    ({"type": "wecom", "webhook": "qyapi.weixin.qq.com/x"}, "http"),
    ({"type": "dingtalk"}, "webhook"),
    (dict(DINGTALK, secret=123), "secret"),
    ({"type": "feishu"}, "webhook"),
    # smtp
    ({"type": "smtp"}, "host"),
    (dict(SMTP, mail_from=""), "mail_from"),
    (dict(SMTP, mail_from="nobody"), "mail_from"),
    (dict(SMTP, mail_to=[]), "mail_to"),
    (dict(SMTP, mail_to="bad-addr"), "mail_to"),
    (dict(SMTP, port=0), "port"),
    (dict(SMTP, port=70000), "port"),
    (dict(SMTP, port="abc"), "port"),
    # 其它
    ({"type": "pager"}, "不支持"),
    ({}, "type"),
    (None, "字典"),
    ("webhook", "字典"),
])
def test_validate_illegal_returns_chinese_message(channel, keyword):
    msg = notify.validate(channel)
    assert isinstance(msg, str) and msg, "非法配置必须返回非空中文说明"
    assert keyword in msg


def test_validate_never_raises_on_hostile_input():
    odd = [None, 1, [], "x", {"type": object()},
           {"type": "smtp", "host": object()},
           {"type": "webhook", "url": {"a": 1}}]
    for ch in odd:
        assert notify.validate(ch) is None or isinstance(notify.validate(ch), str)


def test_validate_smtp_defaults_ok():
    assert notify.validate({"type": "smtp", "host": "h", "mail_from": "a@b",
                            "mail_to": "c@d"}) is None


def test_validate_accepts_string_booleans_and_port():
    ch = dict(SMTP, port="2525", starttls="false", ssl="0")
    assert notify.validate(ch) is None


# ---------------------------------------------------------------- describe

def test_describe_all_types():
    assert notify.describe(WEBHOOK) == "Webhook https://example.com/hook"
    assert notify.describe(WECOM) == "企业微信机器人"
    assert notify.describe(DINGTALK) == "钉钉机器人"
    assert notify.describe(FEISHU) == "飞书机器人"
    assert notify.describe(SMTP) == "SMTP a@b.com → c@d.com"


def test_describe_multi_recipients_and_invalid_configs():
    assert notify.describe(dict(SMTP, mail_to=["a@b", "c@d"])) == "SMTP a@b.com → a@b,c@d"
    for ch in [None, {}, {"type": "pager"}, {"type": "webhook"},
               {"type": "wecom"}, {"type": "smtp"}, 1, []]:
        out = notify.describe(ch)
        assert isinstance(out, str) and out


# ---------------------------------------------------------------- render_payload

def test_render_payload_webhook_json():
    payload = notify.render_payload(WEBHOOK, "标题", "正文")
    assert payload["title"] == "标题"
    assert payload["text"] == "正文"
    assert payload["source"] == "gpm"
    assert isinstance(payload["ts"], int)


def test_render_payload_webhook_text():
    payload = notify.render_payload(WEBHOOK_TEXT, "标题", "正文")
    assert payload == "标题\n正文"
    assert isinstance(payload, str)


def test_render_payload_wecom_markdown():
    payload = notify.render_payload(WECOM, "标题", "正文")
    assert payload == {"msgtype": "markdown",
                       "markdown": {"content": "### 标题\n正文"}}


def test_render_payload_dingtalk_markdown():
    payload = notify.render_payload(DINGTALK, "标题", "正文")
    assert payload == {"msgtype": "markdown",
                       "markdown": {"title": "标题", "text": "正文"}}


def test_render_payload_dingtalk_signed_matches_official_algorithm():
    secret = DINGTALK_SIGNED["secret"]
    payload = notify.render_payload(DINGTALK_SIGNED, "标题", "正文")
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"] == {"title": "标题", "text": "正文"}
    ts, sign = payload["timestamp"], payload["sign"]
    assert ts.isdigit() and sign, "加签字段必须存在且 sign 非空"
    expect = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    assert sign == urllib.parse.quote_plus(base64.b64encode(expect))


def test_render_url_dingtalk_signature_query():
    assert notify.render_url(DINGTALK) == DINGTALK["webhook"]
    url = notify.render_url(DINGTALK_SIGNED)
    assert url.startswith(DINGTALK_SIGNED["webhook"] + "&")
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert qs["timestamp"][0].isdigit()
    assert qs["sign"][0]


def test_render_payload_feishu_structure():
    # 默认走 markdown 变体：飞书官方没有 msg_type=markdown，载荷为 interactive 卡片
    payload = notify.render_payload(FEISHU, "标题", "正文")
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["header"]["title"] == {"tag": "plain_text", "content": "标题"}
    assert payload["card"]["elements"] == [{"tag": "markdown", "content": "正文"}]
    # 渠道配置 markdown=false → 回退纯文本（历史行为）
    legacy = notify.render_payload(dict(FEISHU, markdown=False), "标题", "正文")
    assert legacy == {"msg_type": "text", "content": {"text": "标题\n正文"}}


def test_render_payload_markdown_variants():
    """企微/钉钉/飞书发 markdown 变体：段落标签加粗、裸链接可点；webhook/smtp 纯文本。"""
    text = "【范围】全节点失败（3/3 节点）\n- 规则：可用率\n【链接】https://gpm.example.com/index.html?task=t1&ts=1"
    wecom = notify.render_payload(WECOM, "告警", text)
    assert wecom["msgtype"] == "markdown"
    content = wecom["markdown"]["content"]
    assert content.startswith("### 告警\n")
    assert "**【范围】**" in content and "**【链接】**" in content
    assert "[点击查看](https://gpm.example.com/index.html?task=t1&ts=1)" in content
    assert "- 规则：可用率" in content                        # 普通行原样保留

    ding = notify.render_payload(DINGTALK, "告警", text)
    assert ding["msgtype"] == "markdown" and ding["markdown"]["title"] == "告警"
    assert "**【范围】**" in ding["markdown"]["text"]

    feishu = notify.render_payload(FEISHU, "告警", text)
    assert feishu["msg_type"] == "interactive"
    assert "**【范围】**" in feishu["card"]["elements"][0]["content"]

    # markdown=false 回退：企微/钉钉 msgtype=text
    assert notify.render_payload(dict(WECOM, markdown=False), "标题", "正文") == \
        {"msgtype": "text", "text": {"content": "标题\n正文"}}
    # webhook / smtp 保持纯文本
    assert notify.render_payload(WEBHOOK_TEXT, "标题", "【范围】x") == "标题\n【范围】x"
    assert notify.render_payload(SMTP, "标题", "【范围】x")["body"] == "【范围】x"

    md = notify.render_markdown("标题", "正文\n【持续】已持续 1 分钟")
    assert md == "### 标题\n正文\n**【持续】**已持续 1 分钟"


def test_render_payload_smtp_summary():
    payload = notify.render_payload(SMTP, "标题", "正文")
    assert payload["subject"] == "标题"
    assert payload["body"] == "正文"
    assert payload["mail_from"] == "a@b.com"
    assert payload["mail_to"] == ["c@d.com"]


def test_render_payload_unknown_type_raises():
    with pytest.raises(ValueError):
        notify.render_payload({"type": "pager"}, "t", "b")


# ---------------------------------------------------------------- send: HTTP

def test_send_webhook_json_success(http):
    http.reply(b'{"ok":true}', 200)
    ok, info = notify.send(WEBHOOK, "标题", "正文", timeout=4.0)
    assert ok is True
    assert info == "HTTP 200"
    call = http.calls[0]
    assert call["url"] == "https://example.com/hook"
    assert call["method"] == "POST"
    assert call["timeout"] == 4.0
    assert call["headers"]["Content-type"] == "application/json; charset=utf-8"
    assert http.json_body() == {"title": "标题", "text": "正文", "source": "gpm",
                                "ts": http.json_body()["ts"]}


def test_send_webhook_text_and_custom_headers(http):
    http.reply(b"ok", 200)
    ch = dict(WEBHOOK_TEXT, headers={"X-Token": "abc"})
    ok, info = notify.send(ch, "标题", "正文")
    assert ok is True and info == "HTTP 200"
    call = http.calls[0]
    assert call["data"] == "标题\n正文".encode("utf-8")
    assert call["headers"]["Content-type"] == "text/plain; charset=utf-8"
    assert call["headers"]["X-token"] == "abc"


def test_send_webhook_http_500(http):
    http.fail(_http_error(WEBHOOK["url"], 500, b'{"error":"boom"}'))
    ok, info = notify.send(WEBHOOK, "t", "b")
    assert ok is False
    assert info.startswith("HTTP 500")
    assert "boom" in info


def test_send_timeout_exception(http):
    http.fail(socket.timeout("timed out"))
    ok, info = notify.send(WEBHOOK, "t", "b")
    assert ok is False
    assert "timed out" in info
    assert info.split(":")[0] in ("TimeoutError", "socket.timeout")


def test_send_urlerror_exception(http):
    http.fail(urllib.error.URLError("name resolution failed"))
    ok, info = notify.send(WEBHOOK, "t", "b")
    assert ok is False
    assert info.startswith("URLError")
    assert "name resolution failed" in info


def test_send_wecom_success_and_failure(http):
    http.reply(b'{"errcode":0,"errmsg":"ok"}')
    ok, info = notify.send(WECOM, "标题", "正文")
    assert ok is True and "errcode=0" in info
    assert http.json_body() == {"msgtype": "markdown",
                                "markdown": {"content": "### 标题\n正文"}}

    http.reply(b'{"errcode":93000,"errmsg":"invalid webhook url"}')
    ok, info = notify.send(WECOM, "标题", "正文")
    assert ok is False
    assert "errcode=93000" in info and "invalid webhook url" in info


def test_send_wecom_non_json_response(http):
    http.reply(b"<html>502 Bad Gateway</html>", 200)
    ok, info = notify.send(WECOM, "t", "b")
    assert ok is False
    assert "响应非 JSON" in info and "502 Bad Gateway" in info


def test_send_wecom_missing_errcode(http):
    http.reply(b'{"errmsg":"ok"}')
    ok, info = notify.send(WECOM, "t", "b")
    assert ok is False and "errcode" in info


def test_send_dingtalk_signed_url_and_clean_body(http):
    http.reply(b'{"errcode":0,"errmsg":"ok"}')
    ok, info = notify.send(DINGTALK_SIGNED, "标题", "正文")
    assert ok is True
    url = http.calls[0]["url"]
    assert "timestamp=" in url and "sign=" in url
    body = http.json_body()
    assert body == {"msgtype": "markdown", "markdown": {"title": "标题", "text": "正文"}}
    assert "sign" not in body and "timestamp" not in body


def test_send_dingtalk_errcode(http):
    http.reply(b'{"errcode":310000,"errmsg":"keywords not in content"}')
    ok, info = notify.send(DINGTALK, "t", "b")
    assert ok is False and "errcode=310000" in info


def test_send_feishu_success_and_failure(http):
    http.reply(b'{"code":0,"msg":"success"}')
    ok, info = notify.send(FEISHU, "标题", "正文")
    assert ok is True and "code=0" in info
    body = http.json_body()
    assert body["msg_type"] == "interactive"
    assert body["card"]["elements"] == [{"tag": "markdown", "content": "正文"}]

    # markdown=false 回退纯文本载荷（第 2 次调用）
    http.reply(b'{"code":0,"msg":"success"}')
    ok, info = notify.send(dict(FEISHU, markdown=False), "标题", "正文")
    assert ok is True
    assert http.json_body(idx=1) == {"msg_type": "text", "content": {"text": "标题\n正文"}}

    http.reply(b'{"code":9499,"msg":"Bad Request"}')
    ok, info = notify.send(FEISHU, "标题", "正文")
    assert ok is False and "code=9499" in info and "Bad Request" in info


def test_send_http_error_keeps_status_and_body_head(http):
    body = b"x" * 500
    http.fail(_http_error(WEBHOOK["url"], 502, body))
    ok, info = notify.send(WEBHOOK, "t", "b")
    assert ok is False
    assert info.startswith("HTTP 502")
    assert len(info) <= len("HTTP 502: ") + 200


def test_send_invalid_config_never_touches_network(http):
    ok, info = notify.send({"type": "webhook"}, "t", "b")
    assert ok is False and "配置非法" in info
    ok, info = notify.send({"type": "pager"}, "t", "b")
    assert ok is False and "配置非法" in info
    assert http.calls == []


def test_send_never_raises_for_garbage():
    for ch in [None, [], "webhook", {"type": "wecom", "webhook": None},
               {"type": "smtp", "host": "h", "mail_from": "a@b", "mail_to": ["c@d"]}]:
        res = notify.send(ch, "t", "b", timeout=None)
        assert isinstance(res, tuple) and len(res) == 2
        assert isinstance(res[0], bool) and isinstance(res[1], str)


def test_send_bad_timeout_returns_false(http):
    ok, info = notify.send(WEBHOOK, "t", "b", timeout="abc")
    assert ok is False and "timeout" in info
    assert http.calls == []


# ---------------------------------------------------------------- send: SMTP

def test_send_smtp_default_starttls_and_login(smtp_fakes):
    ok, info = notify.send(SMTP, "标题", "正文", timeout=3.5)
    assert ok is True and "c@d.com" in info
    assert len(FakeSMTP.instances) == 1 and FakeSMTPSSL.instances == []
    srv = FakeSMTP.instances[0]
    assert (srv.host, srv.port, srv.timeout) == ("smtp.example.com", 587, 3.5)
    assert ("starttls",) in srv.calls
    assert ("login", "u", "p") in srv.calls
    assert srv.calls[-1] == ("quit",)
    msg = srv.messages[0]
    assert msg["Subject"] == "标题"
    assert msg["From"] == "a@b.com"
    assert msg["To"] == "c@d.com"
    assert msg.get_content().strip() == "正文"


def test_send_smtp_no_auth_when_no_user(smtp_fakes):
    ch = {"type": "smtp", "host": "h", "mail_from": "a@b.com",
          "mail_to": ["c@d.com", "e@f.com"]}
    ok, _ = notify.send(ch, "t", "b")
    assert ok is True
    srv = FakeSMTP.instances[0]
    assert not any(c[0] == "login" for c in srv.calls)
    assert srv.port == 587
    assert srv.messages[0]["To"] == "c@d.com, e@f.com"


def test_send_smtp_ssl_ignores_starttls(smtp_fakes):
    ch = dict(SMTP, ssl=True, starttls=True)
    ok, _ = notify.send(ch, "t", "b")
    assert ok is True
    assert FakeSMTP.instances == [] and len(FakeSMTPSSL.instances) == 1
    srv = FakeSMTPSSL.instances[0]
    assert not any(c[0] == "starttls" for c in srv.calls)
    assert ("login", "u", "p") in srv.calls


def test_send_smtp_starttls_disabled(smtp_fakes):
    ch = dict(SMTP, starttls=False)
    ok, _ = notify.send(ch, "t", "b")
    assert ok is True
    assert not any(c[0] == "starttls" for c in FakeSMTP.instances[0].calls)


@pytest.mark.parametrize("stage, keyword", [
    ("connect", "OSError"),
    ("starttls", "SMTPException"),
    ("login", "SMTPAuthenticationError"),
    ("send", "SMTPRecipientsRefused"),
])
def test_send_smtp_failure_paths(smtp_fakes, stage, keyword):
    FakeSMTP.fail_at = stage
    if stage == "connect":
        FakeSMTPSSL.fail_at = stage
        ch = dict(SMTP, ssl=True)
    else:
        ch = SMTP
    ok, info = notify.send(ch, "t", "b")
    assert ok is False
    assert info.startswith(keyword)
    if stage != "connect":
        assert FakeSMTP.instances[0].calls[-1] == ("quit",)


def test_send_smtp_invalid_config_does_not_connect(smtp_fakes):
    ok, info = notify.send({"type": "smtp", "host": "h"}, "t", "b")
    assert ok is False and "配置非法" in info
    assert FakeSMTP.instances == [] and FakeSMTPSSL.instances == []


# ---------------------------------------------------------------- 无副作用

def test_no_stdout_output(capsys, monkeypatch):
    http = FakeHTTP().reply(b'{"errcode":0}').install(monkeypatch)
    notify.send(WECOM, "t", "b")
    notify.send({"type": "webhook"}, "t", "b")
    notify.validate(None)
    notify.describe({})
    out, err = capsys.readouterr()
    assert out == "" and err == ""
