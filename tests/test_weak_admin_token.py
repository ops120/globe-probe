"""tests/test_weak_admin_token.py —— 弱 admin token 启动 WARNING（P0-1）。

server.admin_token 配置后写接口要求 X-Admin-Token，但常见把示例值
（change-me-admin / gpm-change-me）或过短值留在配置里 —— 鉴权形同虚设。
约定（app.warn_admin_token_startup）：

1. 命中弱值特征（change-me 前缀、gpm-change-me、长度 < 16）→ 启动打 WARNING：
   说明这是弱 token、写接口将按它鉴权、给出 openssl rand -hex 24 生成提示；
2. 只警告不拒绝启动；环回监听（本地开发）也照样提醒；
3. 强 token / 未配置 → 不打弱值警告（未配置+非环回的 403 提示是另一条，见
   tests/integration/test_write_auth.py）。
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from gpm.config import Config
from gpm.server.app import create_app, warn_admin_token_startup, weak_admin_token_reason

STRONG = "k1f9c0ffee24beef7acc0de13579b2f4"   # 36 字符随机十六进制


def _cfg(admin_token, listen="127.0.0.1:0"):
    return Config({"server": {"database": ":memory:", "listen": listen,
                              "admin_token": admin_token}})


def _weak_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "弱" in r.getMessage()]


def test_weak_token_reason_matrix():
    """特征矩阵：change-me 前缀（含 gpm-change-me、大小写）与 < 16 字符算弱；强值/空不算。"""
    weak_vals = ("change-me-admin", "change-me", "gpm-change-me", "Change-Me-Admin",
                 "GPM-CHANGE-ME", "short", "1234567890abcde")   # 最后一个 15 字符
    for v in weak_vals:
        assert weak_admin_token_reason(v), "%s 应判为弱 token" % v
    strong_vals = ("", None, "a" * 16, STRONG)
    for v in strong_vals:
        assert weak_admin_token_reason(str(v or "")) is None, "%s 不应判为弱 token" % v


def test_weak_token_warns_at_startup_even_on_loopback(caplog):
    """弱 token + 环回监听（本地开发形态）：照样打 WARNING，文案含鉴权说明与生成命令。"""
    with caplog.at_level(logging.WARNING, logger="gpm.server"):
        warn_admin_token_startup(_cfg("change-me-admin"))
    warns = _weak_warnings(caplog)
    assert warns, "配置弱 token 时启动必须打 WARNING"
    msg = warns[0]
    assert "弱 token" in msg, msg
    assert "写接口将按该弱 token 鉴权" in msg, msg
    assert "openssl rand -hex 24" in msg, msg


def test_short_token_warns_too(caplog):
    """过短（< 16 字符）也是弱值：同样警告。"""
    with caplog.at_level(logging.WARNING, logger="gpm.server"):
        warn_admin_token_startup(_cfg("s3cret"))
    assert _weak_warnings(caplog)


def test_strong_token_no_weak_warning(caplog):
    """强 token：不打弱值警告，也不打「未配置」警告。"""
    with caplog.at_level(logging.WARNING, logger="gpm.server"):
        warn_admin_token_startup(_cfg(STRONG))
    assert not _weak_warnings(caplog)
    assert not [r for r in caplog.records if "未配置" in r.getMessage()]


def test_app_startup_lifespan_logs_warning(tmp_path, caplog):
    """端到端：弱 token 启动 app（走 lifespan）也能看到该 WARNING，且服务正常可用。"""
    from gpm.server.storage import Storage
    db = str(tmp_path / "weak-token.db")
    cfg = Config({"server": {"database": db, "listen": "127.0.0.1:0",
                             "admin_token": "gpm-change-me"}})
    with caplog.at_level(logging.WARNING, logger="gpm.server"):
        # TestClient 作上下文管理器才会触发 lifespan（启动检查在那里）
        with TestClient(create_app(cfg, Storage(db))) as client:
            assert client.get("/api/health").json()["ok"] is True
    weak = _weak_warnings(caplog)
    assert weak and "openssl rand -hex 24" in weak[0], \
        "启动 app 应看到弱 token WARNING，实际: %r" % [r.getMessage() for r in caplog.records]
