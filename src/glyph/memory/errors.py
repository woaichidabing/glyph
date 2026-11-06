"""长期记忆错误族（挂在总基类 GlyphError 下，R5）。

由工具层翻译成 ``ToolError`` 后回灌模型，不直接炸循环。
"""

from __future__ import annotations

from ..errors import GlyphError

__all__ = [
    "MemoryError",
    "MemoryEntryNotFoundError",
    "MemoryStoreError",
    "MemoryValidationError",
]


class MemoryError(GlyphError):
    """记忆层错误总基类。"""


class MemoryEntryNotFoundError(MemoryError):
    """按 key 找不到记忆条目。"""


class MemoryStoreError(MemoryError):
    """记忆文件读写失败（磁盘 / 权限 / 文件损坏）。"""


class MemoryValidationError(MemoryError):
    """记忆字段不合法（枚举值、日期、必填项）。"""
