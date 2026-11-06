"""会话层上下文插槽测试（设计稿 §3.8）：合成消息在 system 之后、不落盘、日期每轮重算。"""

from datetime import datetime, timedelta, timezone

from glyph.context import CONTEXT_HEADER, env_date_fragment
from glyph.session import new_session
from glyph.types import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    UserMessage,
)

_TZ = timezone(timedelta(hours=8))
_SYSTEM_PROMPT = "你是一位耐心的英语老师。"


def make_clock(day: int):
    def _clock() -> datetime:
        return datetime(2026, 9, day, 17, 0, 0, tzinfo=_TZ)

    return _clock


async def test_context_message_is_right_after_system(tmp_path) -> None:
    """Bug B：context_message 必须紧跟 SystemMessage、在第一条对话记录之前，
    而不是被无条件追加在末尾（末尾恰为 ToolMessage 时会形成「工具结果→user」的脆弱序列）。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="m",
        cwd="D:\\x",
        root=tmp_path,
        clock=make_clock(25),
        context=[env_date_fragment(clock=make_clock(25))],
    )

    # 加一条真实对话：验证 context 紧跟 system、且不会被挤到末尾
    # （空会话时 context 自然在最后属正常，故此处用真实记录把"末尾"占住）
    await history.record_user_message("Hello there")
    messages = history.messages()

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], UserMessage)
    assert messages[1].content.startswith(CONTEXT_HEADER)
    assert "2026-09-25" in messages[1].content
    # 末尾是真实用户消息，不是合成 context
    assert isinstance(messages[-1], UserMessage)
    assert messages[-1].content == "Hello there"
    assert CONTEXT_HEADER not in messages[-1].content


async def test_context_position_with_tool_call_tool_result_user(tmp_path) -> None:
    """Bug B：多轮 tool_call→tool_result→user 记录下，context 仍在 system 之后，
    且 tool_call / tool_result 配对不受影响、末尾不再多出一条 user。"""
    memory_dir = {"text": "<long_term_memory>笔记 v1</long_term_memory>"}

    def memory_fragment() -> str | None:
        return memory_dir["text"]

    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="m",
        cwd="D:\\x",
        root=tmp_path,
        clock=make_clock(25),
        context=[memory_fragment],
    )
    # 第一轮：user → assistant(带 tool_call) → tool_result → 再来一条 user
    await history.record_user_message("帮我改这句：I has a apple")
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[
                TextBlock(text="我先查一下搭配。"),
                ToolCallBlock(
                    id="call_1", name="grammar_check", arguments={"s": "I has a apple"}
                ),
            ]
        )
    )
    await history.record_tool_result(
        "call_1",
        ToolResult(tool_name="grammar_check", ok=True, output={"tip": "have"}, duration_ms=1.0),
    )
    await history.record_user_message("再来一句")

    messages = history.messages()

    # 0: system, 1: context, 2: user(第一句), 3: assistant(tool_call), 4: tool, 5: user(再来一句)
    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], UserMessage)
    assert messages[1].content.startswith(CONTEXT_HEADER)
    assert "笔记 v1" in messages[1].content
    assert isinstance(messages[2], UserMessage)
    assert messages[2].content == "帮我改这句：I has a apple"
    assert isinstance(messages[3], AssistantMessage)
    assert [c.id for c in messages[3].tool_calls] == ["call_1"]
    assert isinstance(messages[4], ToolMessage)
    assert messages[4].tool_call_id == "call_1"
    assert isinstance(messages[5], UserMessage)
    assert messages[5].content == "再来一句"
    # 末尾是真实 user，不再是合成 context
    assert CONTEXT_HEADER not in messages[-1].content
    # tool_call / tool_result 配对：每个 call 恰好一个结果
    call_ids = [b.id for m in messages if isinstance(m, AssistantMessage) for b in m.tool_calls]
    result_ids = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert sorted(call_ids) == sorted(result_ids)


async def test_context_message_is_never_persisted(tmp_path) -> None:
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="m",
        cwd="D:\\x",
        root=tmp_path,
        clock=make_clock(25),
        context=[env_date_fragment(clock=make_clock(25))],
    )
    # 调一次 messages() 触发合成
    history.messages()

    raw = history.path.read_text(encoding="utf-8")

    assert CONTEXT_HEADER not in raw
    assert "AGENTS.md instructions" not in raw
    assert "今天的日期" not in raw


async def test_no_context_by_default(tmp_path) -> None:
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="m",
        cwd="D:\\x",
        root=tmp_path,
        clock=make_clock(25),
    )

    # 默认无 context：只有 system 一条，末尾不是合成消息
    messages = history.messages()
    assert len(messages) == 1
    assert "AGENTS.md instructions" not in messages[0].content


async def test_context_is_re_rendered_when_date_changes(tmp_path) -> None:
    # 可变时钟：记录条数不变、只跨了一天
    current_day = {"day": 25}

    def shifting_clock() -> datetime:
        return datetime(2026, 9, current_day["day"], 0, 0, 0, tzinfo=_TZ)

    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="m",
        cwd="D:\\x",
        root=tmp_path,
        clock=shifting_clock,
        context=[env_date_fragment(clock=shifting_clock)],
    )

    assert "2026-09-25" in history.messages()[-1].content

    current_day["day"] = 26
    messages = history.messages()

    # 缓存键含日期 ⇒ 跨天后重新渲染，读不到昨天的旧日期
    assert "2026-09-26" in messages[-1].content
    assert "2026-09-25" not in messages[-1].content


async def test_context_is_re_rendered_when_memory_catalog_changes(tmp_path) -> None:
    """Bug C：同一天、记录条数不变，仅长期记忆目录内容变化时，messages() 也必须重渲染
    （旧缓存键只看「记录数 + 日期」，会漏掉记忆目录变化）。"""
    memory = {"text": "<long_term_memory>笔记 v1</long_term_memory>"}

    def memory_fragment() -> str | None:
        return memory["text"]

    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="m",
        cwd="D:\\x",
        root=tmp_path,
        clock=make_clock(25),
        context=[memory_fragment],
    )
    # 固定一条真实记录；之后只改 context、不增删记录、也不跨天
    await history.record_user_message("hi")

    def context_content() -> str:
        for msg in history.messages():
            if isinstance(msg, UserMessage) and msg.content.startswith(CONTEXT_HEADER):
                return msg.content
        return ""

    assert "笔记 v1" in context_content()

    memory["text"] = "<long_term_memory>笔记 v2 完全不同</long_term_memory>"
    rendered = context_content()

    assert "笔记 v2" in rendered
    assert "笔记 v1" not in rendered
