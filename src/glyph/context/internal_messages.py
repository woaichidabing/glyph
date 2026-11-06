"""落盘的「内部 user 消息」约定：给模型看、进账本，但不是用户说的话、不进用户可见回放。

与 ``inject.py`` 的区别：``inject`` 的上下文片段**不落盘**、每轮实时合成；这里的内部
消息由主循环在特定编排场景（如输出被截断后续写）写进账本，以便跨轮重建时仍带这句指令。
共同点是：它们都不是用户发出的——把账本回放给用户看时，内部消息必须隐藏。

设计对标 Claude Code 的 ``isMeta``、Codex 的 "model-visible / user-hidden" 内容：两家
都区分"给模型的内部指令"和"给用户看的对话"，这里用显式判定把这条边界固化。
"""

from __future__ import annotations

__all__ = ["CONTINUATION_PROMPT", "is_internal_user_message"]

# 输出升到最大额度仍被截断时，作为内部 user 消息续给下一轮（实时不 emit、回放不显示）。
# 固定文案、不含任何用户内容，避免把半截原文回灌成提示注入通道（同中断标记的考量）。
CONTINUATION_PROMPT = (
    "（上一条回复因长度限制在中途被截断，请直接从断点继续写完，"
    "不要重复已经说过的内容，也不要加任何开场白或解释。）"
)

# 内部消息集合：新增内部 user 消息时登记到此，回放即自动隐藏。
_INTERNAL_USER_MESSAGES = frozenset({CONTINUATION_PROMPT})


def is_internal_user_message(text: str) -> bool:
    """该 user 文本是否为内部指令：进账本、给模型看，但不进用户可见回放。"""
    return text in _INTERNAL_USER_MESSAGES
