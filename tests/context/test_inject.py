"""context 每轮上下文插槽测试（纯函数，离线）。"""

from datetime import datetime

from glyph.context.inject import (
    CONTEXT_DISCLAIMER,
    CONTEXT_HEADER,
    env_date_fragment,
    render_context_message,
)
from glyph.types import UserMessage


def fixed_clock() -> datetime:
    return datetime(2026, 9, 25, 14, 30, 5)


def test_env_date_fragment_gives_iso_date_only() -> None:
    fragment = env_date_fragment(clock=fixed_clock)

    text = fragment()

    # 只给日期，不给时刻 / 星期几
    assert "2026-09-25" in text
    assert "14:30" not in text
    assert "周五" not in text and "星期" not in text


def test_render_collects_non_empty_fragments_into_one_user_message() -> None:
    msg = render_context_message(
        [
            env_date_fragment(clock=fixed_clock),
            lambda: "<x>记忆目录内容</x>",
        ]
    )

    assert isinstance(msg, UserMessage)
    assert msg.content.startswith(CONTEXT_HEADER)
    assert "2026-09-25" in msg.content
    assert "<x>记忆目录内容</x>" in msg.content
    assert msg.content.rstrip().endswith(CONTEXT_DISCLAIMER)


def test_render_skips_none_and_blank_fragments() -> None:
    msg = render_context_message(
        [
            lambda: None,
            lambda: "   ",
            env_date_fragment(clock=fixed_clock),
        ]
    )

    assert isinstance(msg, UserMessage)
    assert "2026-09-25" in msg.content


def test_render_returns_none_when_all_fragments_empty() -> None:
    assert render_context_message([]) is None
    assert render_context_message([lambda: None, lambda: ""]) is None
