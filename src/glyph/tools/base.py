"""tools/ 侧的词汇表：R10「只读 / 写入身份声明」的落地载体。

背景（架构评审 C-4 已核验）：``AGENTS.md`` R10 要求新增工具标明只读 / 写入。
``readonly`` 定义在 ``runtime/tool.py`` 的 ``Tool`` 协议上（默认 ``False``，
即"未声明一律按写入保守处理"）；新工具作者在本文件二选一继承
``ReadOnlyTool`` / ``WriteTool``，身份一眼可见。``AgentLoop`` 据此决定
批次走并发（全只读）还是串行（含写入）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import ClassVar, TypeVar

from ..runtime.errors import ToolError
from ..runtime.tool import Tool

__all__ = ["ReadOnlyTool", "WriteTool", "guarded"]

_T = TypeVar("_T")


class ReadOnlyTool(Tool):
    """只读工具基类：不动用户文件，允许同批次并发。

    继承它即声明身份（R10）：``AgentLoop`` 对全只读批次保持并发执行。
    """

    readonly: ClassVar[bool] = True


class WriteTool(Tool):
    """写入工具基类：会落盘 / 改写文件，必须串行执行。

    批次里只要出现一个写入工具，整批（含同批只读工具）按模型声明顺序逐个执行、
    两两时间区间严格不重叠（R10）。
    """

    readonly: ClassVar[bool] = False


async def guarded(coro: Awaitable[_T], message: str) -> _T:
    """统一的三段式异常兜底（R5）：取消与 ToolError 原样放行，其余转成 ToolError。

    原来这段只活在 ``ocr.py`` 里；加了第二个看图工具后必须上提到这里，
    否则「哪一处漏了 ``except CancelledError``」这种致命差异只能靠肉眼审 ——
    收成一处后，取消信号的正确性只有一份代码要维护。
    """
    try:
        return await coro
    except asyncio.CancelledError:
        raise
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成 ToolError（R5）
        raise ToolError(f"{message}：{_humanize(exc)}") from exc


# 常见厂商报错 → 人话。模型会把这段原样转述给用户，裸 JSON 他看不懂也无处下手；
# 只替换「有明确解法」的少数几种，认不出的照旧带原文（排查要用）。
_MODEL_MISSING_HINT = (
    "配置的模型在这个账号/服务端不存在。"
    "请在 .env 里把对应模型换成账号可用的（视觉模型是 GLYPH_VISION_MODEL），改完重启后端再试"
)
_HUMANIZE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("model_not_found", _MODEL_MISSING_HINT),
    ("model not exist", _MODEL_MISSING_HINT),
)


def _humanize(exc: Exception) -> str:
    """认得出的问题给人话加解法，认不出的保留原始报错（排查要用）。"""
    text = str(exc)
    for needle, hint in _HUMANIZE_PATTERNS:
        if needle.lower() in text.lower():
            return hint
    return text
