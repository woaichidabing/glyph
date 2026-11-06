"""运行时错误类型（铁律 R5）。

- ``GlyphError``：全项目错误总基类，已上移到顶层 ``glyph.errors``。这里 **re-export**
  仅为兼容旧导入路径 ``from glyph.runtime.errors import GlyphError``，新代码请从
  ``glyph.errors`` 导入。
- ``ToolError``：工具作者主动表达"这件事业务上做不成"（如缺少前置条件）。注册表捕获后
  转成一条 ``ok=False`` 的工具结果回灌模型，循环继续。
- 其他意外异常由注册表统一包装为错误结果，不允许裸抛、不允许静默吞掉。
"""

from __future__ import annotations

from ..errors import GlyphError

__all__ = ["GlyphError", "ToolError"]


class ToolError(GlyphError):
    """工具业务错误。消息应当是能让模型读懂、可据此改参数或换工具的人话。"""
