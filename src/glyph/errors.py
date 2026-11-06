"""Glyph 全项目统一错误基类（铁律 R5）。

放在最顶层、**不依赖任何项目内模块**，让 runtime / llm / session 各层都把自己的错误族
挂到它下面，而层与层之间不产生反向引用：

- runtime 的 ``ToolError``、llm 的 ``LLMError``、session 的 ``SessionError`` 都继承它；
- 调用方既能按某一层的粒度捕获，也能用一句 ``except GlyphError`` 兜住全部。

各层特有的错误仍定义在各自包里（例如 ``runtime/errors.py`` 的 ``ToolError``）；那些模块
会 re-export 本基类，使旧的 ``from glyph.runtime.errors import GlyphError`` 仍然可用。
"""

from __future__ import annotations


class GlyphError(Exception):
    """Glyph 所有可预期错误的总基类。消息一律写成能直接给人看的话。"""
