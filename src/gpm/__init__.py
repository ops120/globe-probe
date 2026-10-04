"""gpm — globe-probe 全球拨测监控平台

版本号不再硬编码：优先用 setuptools_scm 注入的 __version__（来自 git tag /
commit），打包 / 安装 / 运行 / 调试都是同一份事实。运行时在没有 PKG-INFO
（开发模式直接 python -m gpm）的场景兜底读 git describe，最后再退到
DEFAULT_VERSION。

下游消费方：
  - /api/health 返回 "version": __version__
  - 启动日志用 version 标识
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _resolve_version() -> str:
    """按优先级探测版本号（绝不写死）。"""
    DEFAULT_VERSION = "0.1.0"

    # 1) setuptools_scm 显式 write_to 场景：导入包时已生成 _version.py
    try:
        from ._version import version as _pkg_version
        if _pkg_version and _pkg_version != "0+unknown":
            return _pkg_version
    except Exception:
        pass

    # 1.5) pip 安装后（含 PEP 660 editable）：版本在发行元数据里，源码树没有
    # _version.py——曾经漏了这条，导致「pip show 是 0.1.1.dev27、import 却报 git
    # 短 hash」两套口径并存
    try:
        from importlib.metadata import version as _meta_version
        return _meta_version("gpm")
    except Exception:
        pass

    # 2) dev 模式直接 python -m gpm：兜底 git describe
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--dirty", "--always"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        s = out.decode("utf-8", "replace").strip()
        if s:
            return s.lstrip("v").split("-dirty")[0] or DEFAULT_VERSION
    except Exception:
        pass

    # 3) 环境变量兜底（CI / 容器构建期可覆盖）
    return os.environ.get("GPM_VERSION") or DEFAULT_VERSION


__version__ = _resolve_version()
