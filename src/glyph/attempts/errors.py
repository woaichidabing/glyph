"""学习尝试记录错误族（挂在总基类 GlyphError 下，R5）。

由工具层翻译成 ``ToolError`` 后回灌模型，不直接炸主循环。
"""

from __future__ import annotations

from ..errors import GlyphError

__all__ = [
    "AttemptError",
    "AttemptNotFoundError",
    "AttemptStoreError",
    "AttemptValidationError",
]


class AttemptError(GlyphError):
    """尝试记录层错误总基类。"""


class AttemptNotFoundError(AttemptError):
    """按 attempt_id 找不到尝试记录。"""


class AttemptStoreError(AttemptError):
    """尝试记录文件读写失败（磁盘 / 权限 / 文件损坏）。"""


class AttemptValidationError(AttemptError):
    """尝试记录字段不合法（缺字段、枚举值错、确认闸门为空）。"""
