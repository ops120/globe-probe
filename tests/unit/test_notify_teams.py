"""notify Teams 出向渠道单元测试：MessageCard 载荷 + 发送路径（HTTP 打桩）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gpm.server import notify  # noqa: E402


def test_validate_teams_channel():
    assert notify.validate({"type": "teams", "url": "https://outlook.office.com/webhook/xxx"}) is None
    err = notify.validate({"type": "teams"})
    assert err and "url" in err
    err = notify.validate({"type": "teams", "url": "not-a-url"})
    assert err
    # 不在支持列表的类型仍被拒
    assert notify.validate({"type": "slack", "url": "https://hooks.slack.com/x"})


def test_describe_teams():
    assert notify.describe({"type": "teams", "url": "https://x"}) == "Teams 机器人"
    assert "缺少" in notify.describe({"type": "teams"})


def test_send_teams_builds_message_card(monkeypatch):
    """MessageCard 形态：summary 必填、title/text 直传、POST 到渠道 url。"""
    seen = {}

    def fake_http(url, data, timeout, headers, method):
        seen["url"], seen["data"], seen["headers"], seen["method"] = url, data, headers, method
        return True, 200, "OK"

    monkeypatch.setattr(notify, "_http", fake_http)
    ok, msg = notify.send({"type": "teams", "url": "https://prod-xx.office.com/webhook/abc"},
                          "【gpm】相关簇 #1", "疑似节点侧：3 条故障同落 win-local")
    assert ok and "HTTP 200" in msg
    assert seen["url"] == "https://prod-xx.office.com/webhook/abc"
    assert seen["method"] == "POST"
    card = json.loads(seen["data"])
    assert card["@type"] == "MessageCard"
    assert card["summary"] == "【gpm】相关簇 #1"
    assert "疑似节点侧" in card["text"]


def test_send_teams_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(notify, "_http",
                        lambda *a, **k: (False, 500, "HTTP 500: bad gateway"))
    ok, msg = notify.send({"type": "teams", "url": "https://x"}, "t", "x")
    assert ok is False and "500" in msg
