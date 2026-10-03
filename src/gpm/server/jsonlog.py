"""结构化 JSON 日志（ONCALL_OPTIMIZATION.md 第三期 13：错误日志结构化）。

默认关闭、行为与原来完全一致；设环境变量 ``GPM_LOG_JSON=1``（true/yes/on 同义）
后，gpm 与 uvicorn 的日志改为单行 JSON，排障从「读全文」变成「按字段过滤」::

    {"ts": "2026-10-02T12:00:00.123", "level": "INFO", "logger": "gpm.server",
     "message": "服务端启动: ...", "task_id": "...", "node_id": "..."}

约定：
- 日志调用里 ``extra={"task_id": ..., "node_id": ...}`` 等业务键平铺到顶层
  （JSON 序列化不了的值降级为 repr，绝不让日志调用本身失败）；
- ``exc_info`` 输出为多行字符串字段 ``exc``；
- 只换 formatter、不新增 handler、不动级别——路由与采集配置保持原样；
- 装配：``app.create_app`` 与 lifespan 各调一次 :func:`setup`（幂等）。调两次是
  故意的：``uvicorn.run()`` 会在 create_app 之后用自己的 dictConfig 重置
  ``uvicorn.*`` 的 handler，lifespan（真正起服务时才走）里再接一次才能覆盖
  uvicorn 自己的启动/访问日志。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

#: 开关环境变量；未设置/空 = 关闭（保持原有纯文本行为）
ENV_FLAG = "GPM_LOG_JSON"

#: LogRecord 的标准属性不进 JSON 平铺（时间/级别/消息已单独表达）
_STD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs", "message",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "stacklevel", "taskName", "thread", "threadName",
})


def _iso(created: float, msecs: float) -> str:
    """本地时区 ISO 时间戳（含毫秒）。"""
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created))
    return f"{base}.{int(msecs):03d}"


class JsonFormatter(logging.Formatter):
    """logging.Formatter → 单行 JSON（ensure_ascii=False，中文原样输出）。"""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": _iso(record.created, record.msecs),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_") or key in entry:
                continue
            try:
                json.dumps(val)
            except (TypeError, ValueError):
                val = repr(val)
            entry[key] = val
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=repr)


def _flag(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def enabled() -> bool:
    """GPM_LOG_JSON 是否启用（未设置=关闭）。"""
    return _flag(os.environ.get(ENV_FLAG))


def setup(force: bool | None = None) -> bool:
    """把 JSON formatter 接到现有 handler 上（幂等）。返回是否启用。

    force 缺省按环境变量判定；显式 True/False 可在测试或将来加配置项时绕过环境。
    """
    on = enabled() if force is None else force
    if not on:
        return False
    fmt: logging.Formatter = JsonFormatter()
    seen: list[int] = []
    for name in (None, "gpm", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name) if name is not None else logging.getLogger()
        for h in lg.handlers:
            if id(h) in seen:
                continue
            seen.append(id(h))
            h.setFormatter(fmt)
    return True
