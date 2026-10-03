"""JSON 日志 formatter 单测：GPM_LOG_JSON 开关门控 + 输出形状（monkeypatch env）。"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from gpm.server import jsonlog  # noqa: E402


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _logger(name: str, handler: logging.Handler) -> logging.Logger:
    lg = logging.getLogger(name)
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    return lg


def _snapshot_formatters() -> list[tuple[logging.Handler, logging.Formatter | None]]:
    """setup() 会原地改 handler 的 formatter → 测试前后快照/恢复，避免污染别的用例。"""
    out = []
    for name in (None, "gpm", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name) if name is not None else logging.getLogger()
        for h in lg.handlers:
            out.append((h, h.formatter))
    return out


def _restore_formatters(snap) -> None:
    for h, f in snap:
        h.setFormatter(f)


# ---------------------------------------------------------------- formatter 形状


def test_json_formatter_shape_with_extras():
    h = _ListHandler()
    h.setFormatter(jsonlog.JsonFormatter())
    lg = _logger("gpm.jsonlog-shape", h)
    lg.info("探测失败 %s", "timeout", extra={"task_id": "t1", "node_id": "n1"})
    entry = json.loads(h.lines[0])
    assert entry["level"] == "INFO" and entry["logger"] == "gpm.jsonlog-shape"
    assert entry["message"] == "探测失败 timeout"
    assert entry["task_id"] == "t1" and entry["node_id"] == "n1"
    assert entry["ts"].startswith("20") and "T" in entry["ts"]
    # 单行 JSON（server.err.log 按 grep/过滤消费）
    assert len(h.lines[0].splitlines()) == 1


def test_json_formatter_unserializable_extra_becomes_repr():
    h = _ListHandler()
    h.setFormatter(jsonlog.JsonFormatter())
    lg = _logger("gpm.jsonlog-weird", h)
    lg.warning("x", extra={"weird": {1, 2}, "nested": {"a": object()}})
    entry = json.loads(h.lines[0])   # 反序列化本身成功 = 输出是合法 JSON
    # repr 降级发生在 extra 顶层值：set → "{1, 2}"，含不可序列化成员的 dict → 整体 repr
    assert isinstance(entry["weird"], str)
    assert isinstance(entry["nested"], str) and "object" in entry["nested"]


def test_json_formatter_exception_field():
    h = _ListHandler()
    h.setFormatter(jsonlog.JsonFormatter())
    lg = _logger("gpm.jsonlog-exc", h)
    try:
        raise ValueError("boom-value")
    except ValueError:
        lg.exception("处理失败")
    entry = json.loads(h.lines[0])
    assert "ValueError" in entry["exc"] and "boom-value" in entry["exc"]
    assert entry["message"] == "处理失败"


# ---------------------------------------------------------------- setup 门控


def test_flag_values(monkeypatch):
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv(jsonlog.ENV_FLAG, v)
        assert jsonlog.enabled() is True, v
    for v in ("0", "false", "Off", "", "no"):
        monkeypatch.setenv(jsonlog.ENV_FLAG, v)
        assert jsonlog.enabled() is False, v
    monkeypatch.delenv(jsonlog.ENV_FLAG, raising=False)
    assert jsonlog.enabled() is False


def test_setup_gated_by_env_and_idempotent(monkeypatch):
    """setup 只装配「gpm」logger 自身的 handler（生产里 gpm.* 子 logger 全部向上传播）。"""
    monkeypatch.delenv(jsonlog.ENV_FLAG, raising=False)
    snap = _snapshot_formatters()
    h = _ListHandler()
    lg = logging.getLogger("gpm")
    old_handlers, old_level = list(lg.handlers), lg.level
    lg.handlers = [h]
    lg.setLevel(logging.INFO)
    try:
        # 默认关闭：setup 不动 formatter，输出保持纯文本
        assert jsonlog.setup() is False
        lg.info("plain line")
        assert h.lines[-1] == "plain line"
        # 开启：handler 换成 JsonFormatter（幂等，重复 setup 不复制 handler）
        monkeypatch.setenv(jsonlog.ENV_FLAG, "1")
        assert jsonlog.setup() is True
        assert jsonlog.setup() is True
        assert isinstance(h.formatter, jsonlog.JsonFormatter)
        assert len(lg.handlers) == 1
        lg.info("json line %s", "arg", extra={"task_id": "t9"})
        entry = json.loads(h.lines[-1])
        assert entry["message"] == "json line arg" and entry["task_id"] == "t9"
    finally:
        lg.handlers = old_handlers
        lg.setLevel(old_level)
        _restore_formatters(snap)


def test_setup_force_overrides_env(monkeypatch):
    monkeypatch.delenv(jsonlog.ENV_FLAG, raising=False)
    snap = _snapshot_formatters()
    h = _ListHandler()
    lg = logging.getLogger("gpm")
    old_handlers, old_level = list(lg.handlers), lg.level
    lg.handlers = [h]
    lg.setLevel(logging.INFO)
    try:
        assert jsonlog.setup(force=True) is True
        assert isinstance(h.formatter, jsonlog.JsonFormatter)
        assert jsonlog.setup(force=False) is False   # 显式关闭只返回 False，不破坏现状
        assert isinstance(h.formatter, jsonlog.JsonFormatter)
    finally:
        lg.handlers = old_handlers
        lg.setLevel(old_level)
        _restore_formatters(snap)


def test_setup_covers_root_gpm_and_uvicorn_loggers(monkeypatch):
    monkeypatch.setenv(jsonlog.ENV_FLAG, "1")
    snap = _snapshot_formatters()
    handlers = {name: _ListHandler() for name in
                (None, "gpm", "uvicorn", "uvicorn.error", "uvicorn.access")}
    keep = {}
    for name, h in handlers.items():
        lg = logging.getLogger(name) if name is not None else logging.getLogger()
        keep[name] = (lg, list(lg.handlers))
        lg.handlers = [h]
    try:
        assert jsonlog.setup() is True
        for h in handlers.values():
            assert isinstance(h.formatter, jsonlog.JsonFormatter)
    finally:
        for lg, old in keep.values():
            lg.handlers = old
        _restore_formatters(snap)
