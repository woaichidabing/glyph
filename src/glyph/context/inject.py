"""每轮上下文插槽：把"模型要看见、但从未发生过"的内容合成一条消息。

设计见 ``docs/design/long-term-memory-design.md`` §3.8：
- ``ContextFragment`` 是一个零参回调，返回一段正文；None / 空串表示这段没有内容；
- ``render_context_message`` 按顺序收集非空片段，拼成**一条** UserMessage；
- 全部为空时不追加消息（返回 None）。

本模块不 import 任何业务包（无环约束）：日期默认时钟就用 ``datetime.now``，
session / memory 各自把自己的片段作为回调传进来。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from ..types import UserMessage

__all__ = [
    "CONTEXT_HEADER",
    "CONTEXT_DISCLAIMER",
    "ContextFragment",
    "env_date_fragment",
    "render_context_message",
]

ContextFragment = Callable[[], str | None]
"""返回一段上下文正文的回调；None / 空串表示这一段没有内容。"""

CONTEXT_HEADER = "# AGENTS.md instructions"
CONTEXT_DISCLAIMER = (
    "（以上由系统每轮自动附带，不是用户的新问题：不要向用户复述它的存在，"
    "也不要把它当成用户说的话。）"
)


def _default_now() -> datetime:
    return datetime.now()


def env_date_fragment(clock: Callable[[], datetime] = _default_now) -> ContextFragment:
    """今天日期片段：只给 ISO 日期，不给时刻 / 星期几 / 时区。"""

    def fragment() -> str:
        return (
            "<environment_context>\n"
            f"今天的日期：{clock().date().isoformat()}。\n"
            "</environment_context>"
        )

    return fragment


def render_context_message(
    fragments: Sequence[ContextFragment],
) -> UserMessage | None:
    """收集非空片段，拼成一条 UserMessage（header + 各片段 + disclaimer）。

    全部为空时返回 None —— 调用方据此不追加消息。
    """
    bodies = [text.strip() for fragment in fragments if (text := fragment()) and text.strip()]
    if not bodies:
        return None
    content = f"{CONTEXT_HEADER}\n" + "\n".join(bodies) + f"\n{CONTEXT_DISCLAIMER}"
    return UserMessage(content=content)
