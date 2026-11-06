"""Glyph 长期记忆层（本地、单学习者、无登录）。

一条记忆 = 一个 markdown 文件；frontmatter 是真相源，MEMORY.md 目录页只是写入后
重渲染的副产品。本包导出领域模型、存储、目录注入器、错误族与路径单点。
"""

from __future__ import annotations

from .errors import (
    MemoryEntryNotFoundError,
    MemoryError,
    MemoryStoreError,
    MemoryValidationError,
)
from .inject import MemoryCatalogInjector
from .paths import entry_path, memory_root, trash_dir, trash_path
from .store import MemoryStore
from .types import MEMORY_VERSION, MemoryEntry, MemoryType, TimeHint, WriteAction

__all__ = [
    "MEMORY_VERSION",
    "MemoryCatalogInjector",
    "MemoryError",
    "MemoryEntry",
    "MemoryEntryNotFoundError",
    "MemoryStore",
    "MemoryStoreError",
    "MemoryType",
    "MemoryValidationError",
    "TimeHint",
    "WriteAction",
    "entry_path",
    "memory_root",
    "trash_dir",
    "trash_path",
]
