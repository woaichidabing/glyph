"""生词本 / 查词历史的落盘路径单点（纯函数，不读盘）。

- 根目录：~/.markr/wordbook，可用环境变量 GLYPH_WORDBOOK_ROOT 覆盖（测试注入用）；
- 单文件 state.json：items + history 一起存，写入走临时文件 + os.replace
  原子替换，避免前端读到半截 JSON。

路径规则对齐 session/paths.py、memory/paths.py：环境变量只在本模块读一次（R1）。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import data_root

GLYPH_WORDBOOK_ROOT_ENV = "GLYPH_WORDBOOK_ROOT"
"""生词本根目录环境变量名。全项目只有 wordbook_root() 读它（R1）。"""

STATE_FILENAME = "state.json"

__all__ = [
    "GLYPH_WORDBOOK_ROOT_ENV",
    "STATE_FILENAME",
    "state_path",
    "wordbook_root",
]


def wordbook_root() -> Path:
    """生词本根目录：环境变量优先，否则 ~/.markr/wordbook。"""
    raw = os.environ.get(GLYPH_WORDBOOK_ROOT_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_root() / "wordbook"


def state_path(root: str | Path) -> Path:
    """根目录下的单状态文件：<root>/state.json。"""
    return Path(root) / STATE_FILENAME
