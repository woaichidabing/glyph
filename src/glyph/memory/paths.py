"""记忆落盘路径计算（纯函数，不读盘）。

沿用 ``session/paths.py`` 的 R1 模式：
- 根目录：``~/.markr/memory``，可用 ``GLYPH_MEMORY_ROOT`` 覆盖（测试注入用）；
- 一条记忆：``<root>/<type>-<key12>.md``，文件名内容寻址、平铺不分层
  （一个人一辈子能写多少条？N 千以内平铺无压力，省掉索引）；
- 软删：``<root>/.trash/<type>-<key12>-<YYYYmmdd-HHMMSS>.md``。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import data_root
from .types import MemoryType

__all__ = [
    "GLYPH_MEMORY_ROOT_ENV",
    "memory_root",
    "trash_dir",
    "entry_path",
    "trash_path",
    "reviews_dir",
    "review_log_path",
]

GLYPH_MEMORY_ROOT_ENV = "GLYPH_MEMORY_ROOT"
"""记忆根目录环境变量名。全项目只有 ``memory_root()`` 读它（R1）。"""

MEMORY_FILE_SUFFIX = ".md"


def memory_root() -> Path:
    """记忆根目录：环境变量优先，否则 ``~/.markr/memory``。"""
    raw = os.environ.get(GLYPH_MEMORY_ROOT_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_root() / "memory"


def trash_dir(root: Path) -> Path:
    """回收站目录：固定在根下的 .trash（点开头，扫描条目时自动忽略）。"""
    return root / ".trash"


def entry_path(root: Path, type_: MemoryType, key: str) -> Path:
    """活条目路径：``<root>/<type>-<key>.md``。"""
    return root / f"{type_.value}-{key}{MEMORY_FILE_SUFFIX}"


def trash_path(root: Path, type_: MemoryType, key: str, stamped_name: str) -> Path:
    """回收站路径：同名追加时间戳，绝不覆盖。"""
    return trash_dir(root) / stamped_name


def reviews_dir(root: Path) -> Path:
    """复测流水目录：根下 ``reviews/`` 子目录。

    条目扫描只 ``glob('*.md')`` 顶层，子目录里的 jsonl 天然不进记忆列表，与 ``.trash``
    同一隔离思路。
    """
    return root / "reviews"


def review_log_path(root: Path, key: str) -> Path:
    """一个知识点的复测流水：``<root>/reviews/<key12>.jsonl``，append-only、每行一条 JSON。"""
    return reviews_dir(root) / f"{key}.jsonl"
