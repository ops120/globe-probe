"""pytest 全局准备：把 src/ 加入 sys.path。

各测试文件本就该能**单独运行**（pytest tests/unit/test_x.py），此前依赖别处
（如集成测试）先插入 sys.path 的副作用，单跑时会 ModuleNotFoundError。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
