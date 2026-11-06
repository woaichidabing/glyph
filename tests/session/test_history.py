"""session/history.py 测试：会话对象本身的全部对外行为。

切片 1（S4）：new_session 建文件、写首行 session_meta。
切片 2（S4）：meta 里该有的字段都在（含 learner_id 占位）。
切片 3（S4）：open_session 按 id 打开已存在的会话。
切片 4（S4）：文件不在 / 首行不是 meta，各自报对应的错。
切片 5（S5）：record_* 落盘 + messages() 上下文重建。
切片 5b（D18）：取消后的上下文修复 —— 补中断标记，不回灌半截文本。
切片 5c：合成消息的身份 —— 只活在上下文里、永不进流水的那几类。
切片 6（S6）：close 收尾与「关掉再打开一致」。
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from glyph.runtime.history_port import HistoryPort
from glyph.session import SessionHistory, new_session, open_session
from glyph.session.errors import SessionCorruptError, SessionNotFoundError, SessionWriteError
from glyph.session.history import SYNTHETIC_TEXTS, SyntheticKind
from glyph.session.records import (
    APP_VERSION,
    SCHEMA_VERSION,
    CompactBoundaryPayload,
    Record,
    SessionMeta,
    UserMessagePayload,
    build_record,
    system_prompt_fingerprint,
)
from glyph.types import (
    AssistantMessage,
    FinishReason,
    Message,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    Usage,
    UserMessage,
)

_TZ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 9, 19, 17, 50, 3, tzinfo=_TZ)
_SYSTEM_PROMPT = "你是一位耐心的英语老师，批改时先说哪里好，再改一处。"
_MODEL = "qwen-plus"
_CWD = "D:\\markr"


def _clock() -> datetime:
    """定死的时钟（R6 可注入），会话 id 里的日期才好断言。"""
    return _NOW


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


def _rendered_text(messages: list[Message]) -> str:
    """把 messages() 的返回值压平成一个字符串，方便断言某段文案有没有出现。

    只看**会发给模型的正文**（system / user / assistant 文本 / tool 结果正文），
    工具调用本身只取名字——它是结构不是内容。
    """
    parts: list[str] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            parts.append(message.text)
            parts.extend(call.name for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            parts.append(message.content)
        elif isinstance(message, (SystemMessage, UserMessage)):
            parts.append(message.content)
    return "\n".join(parts)


# ---------- 切片 1：新建 ----------


async def test_new_session_writes_session_meta_as_first_line(tmp_path) -> None:
    """建会话的第一件事是把身份证写到第一行，且 seq 恒为 0。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT, model=_MODEL, cwd=_CWD, root=tmp_path, clock=_clock
    )

    assert history.path.exists()
    assert history.path == tmp_path / "2026" / "09" / "19" / f"{history.session_id}.jsonl"

    lines = _lines(history.path)
    assert len(lines) == 1  # 只写 meta，不写别的

    record = Record.model_validate_json(lines[0])
    assert record.seq == 0
    assert record.type == "session_meta"
    assert record.v == SCHEMA_VERSION
    assert record.payload_as(SessionMeta).session_id == history.session_id


async def test_new_session_id_carries_the_injected_timestamp(tmp_path) -> None:
    """会话 id 里的时间来自注入的时钟：session-<YYYYmmdd-HHMMSS>-<短 id>。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT, model=_MODEL, cwd=_CWD, root=tmp_path, clock=_clock
    )

    _, date_part, time_part, short_id = history.session_id.split("-")
    assert (date_part, time_part) == ("20260919", "175003")
    assert len(short_id) == 6


# ---------- 切片 2：meta 字段 ----------


async def test_new_session_meta_contains_learner_id_placeholder(tmp_path) -> None:
    """learner_id 第一版恒为 None（预留一机多人），其余字段按 §2.3 就位。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT, model=_MODEL, cwd=_CWD, root=tmp_path, clock=_clock
    )
    meta = history.session_meta

    assert meta.learner_id is None  # 预留位第一版不填
    assert meta.schema_version == SCHEMA_VERSION
    assert meta.app_version == APP_VERSION
    assert meta.session_id == history.session_id
    assert meta.model == _MODEL
    assert meta.cwd == _CWD
    assert meta.created_at == _NOW.isoformat()
    # 提示词只留指纹，正文不落盘
    assert meta.system_prompt_fingerprint == system_prompt_fingerprint(_SYSTEM_PROMPT)
    assert _SYSTEM_PROMPT not in history.path.read_text(encoding="utf-8")


# ---------- 切片 3：打开 ----------


async def test_open_session_restores_records_by_id(tmp_path) -> None:
    """按 id 重开：读回的还是那一个文件、同一份 meta，且不顺手写新东西进去。"""
    created = await new_session(
        system_prompt=_SYSTEM_PROMPT, model=_MODEL, cwd=_CWD, root=tmp_path, clock=_clock
    )

    reopened = await open_session(
        created.session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock
    )

    assert reopened.session_id == created.session_id
    assert reopened.path == created.path
    assert reopened.session_meta == created.session_meta
    assert len(_lines(reopened.path)) == 1  # 只有首行 meta 被读回，没有重复写一遍


async def test_open_session_flags_system_prompt_mismatch(tmp_path) -> None:
    """重开时换了提示词：指纹对不上就记一笔（第一版不落盘、不报错）。"""
    created = await new_session(
        system_prompt=_SYSTEM_PROMPT, model=_MODEL, cwd=_CWD, root=tmp_path, clock=_clock
    )

    same_prompt = await open_session(
        created.session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock
    )
    changed_prompt = await open_session(
        created.session_id, system_prompt="换一份提示词", root=tmp_path, clock=_clock
    )

    assert same_prompt.system_prompt_mismatch is False
    assert changed_prompt.system_prompt_mismatch is True


async def test_open_session_exposes_skipped_line_count(tmp_path) -> None:
    """重开时跳过了几行坏数据必须透出来 —— 静默丢数据比当场报错更难排查。"""
    created = await _open_new_session(tmp_path)
    await created.record_user_message("第一句")
    # 手工模拟一次崩溃：文件末尾留下一条写了一半的行
    with created.path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"v":1,"seq":2,"ts":"2026-09-19T17:50:11.402+08:00","type":"user_mes')

    reopened = await open_session(
        created.session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock
    )

    assert reopened.skipped_lines == 1
    # 坏行只丢它自己，前面完整的记录照常重建
    assert [json.loads(line)["seq"] for line in _lines(reopened.path)[:2]] == [0, 1]


# ---------- 切片 4：出错 ----------


async def test_open_session_missing_file_raises_not_found(tmp_path) -> None:
    """按 id 找不到文件：报「找不到」，让调用方决定是开新的还是提示用户。"""
    with pytest.raises(SessionNotFoundError):
        await open_session(
            "session-19990101-000000-abcdef", system_prompt=_SYSTEM_PROMPT, root=tmp_path
        )


async def test_open_session_without_meta_raises_corrupt(tmp_path) -> None:
    """首行不是 session_meta：拒绝打开，避免把别人的半截文件当成会话续下去。"""
    session_id = "session-20260919-175003-8f3c2a"
    path = tmp_path / "2026" / "09" / "19" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    orphan = build_record(
        seq=0, ts=_NOW.isoformat(), payload=UserMessagePayload(text="没有身份证的第一行")
    )
    path.write_text(orphan.to_json_line() + "\n", encoding="utf-8")

    with pytest.raises(SessionCorruptError):
        await open_session(session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path)


# ---------- 切片 5：记录方法 + 上下文重建（S5）----------


async def _open_new_session(root: Path) -> SessionHistory:
    """统一的测试夹具：一个定死时钟的新会话。"""
    return await new_session(
        system_prompt=_SYSTEM_PROMPT, model=_MODEL, cwd=_CWD, root=root, clock=_clock
    )


def _file_types(history: SessionHistory) -> list[str]:
    """落盘流水里的事件类型序列（从文件读，验证「确实写到盘上了」）。"""
    return [json.loads(line)["type"] for line in _lines(history.path)]


def _assert_every_tool_call_has_exactly_one_result(messages: list) -> None:
    """发给模型前的不变量：每个 tool_call 都有且只有一个结果，否则千问会报 400。"""
    call_ids = [
        block.id
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.tool_calls
    ]
    result_ids = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]

    assert len(result_ids) == len(set(result_ids))
    assert set(call_ids) == set(result_ids)
    # 结果必须排在它对应的那条 assistant 之后
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage):
            for block in message.tool_calls:
                result_index = next(
                    i
                    for i, m in enumerate(messages)
                    if isinstance(m, ToolMessage) and m.tool_call_id == block.id
                )
                assert result_index > index


async def test_messages_starts_with_system_prompt(tmp_path) -> None:
    """messages() 的第一条永远是系统提示词；include_system=False 时一条都不带。"""
    history = await _open_new_session(tmp_path)

    messages = history.messages()

    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == _SYSTEM_PROMPT
    assert len(messages) == 1  # 只有系统提示词：meta 不进上下文
    assert history.messages(include_system=False) == []


async def test_record_user_message_appends_user_message(tmp_path) -> None:
    """用户说话：进上下文（用户消息），也进流水。"""
    history = await _open_new_session(tmp_path)

    await history.record_user_message("帮我改这句：I has a apple")

    messages = history.messages()
    assert isinstance(messages[1], UserMessage)
    assert messages[1].content == "帮我改这句：I has a apple"
    assert _file_types(history) == ["session_meta", "user_message"]


async def test_record_assistant_message_expands_text_and_tool_calls(tmp_path) -> None:
    """一条 assistant 消息 = 一行文本 + 每个工具调用各一行，顺序保持。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句：I has a apple")

    await history.record_assistant_message(
        AssistantMessage(
            blocks=[
                TextBlock(text="我先查一下这句的主谓搭配。"),
                ToolCallBlock(
                    id="call_8f21",
                    name="grammar_check",
                    arguments={"sentence": "I has a apple"},
                ),
            ]
        )
    )

    assert _file_types(history) == [
        "session_meta",
        "user_message",
        "assistant_message",
        "tool_call",
    ]
    lines = [json.loads(line) for line in _lines(history.path)]
    assert lines[3]["payload"] == {
        "id": "call_8f21",
        "name": "grammar_check",
        "arguments": {"sentence": "I has a apple"},
    }

    assistant = history.messages()[2]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.text == "我先查一下这句的主谓搭配。"
    assert [call.id for call in assistant.tool_calls] == ["call_8f21"]


async def test_record_tool_result_pairs_with_call_id(tmp_path) -> None:
    """工具结果按 call_id 配对：成功给 JSON，失败给人话原因（都算配对）。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句：I has a apple")
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[
                ToolCallBlock(
                    id="call_8f21",
                    name="grammar_check",
                    arguments={"sentence": "I has a apple"},
                )
            ]
        )
    )

    await history.record_tool_result(
        "call_8f21",
        ToolResult(
            tool_name="grammar_check",
            ok=True,
            output={"issues": [{"type": "subject_verb", "tip": "I → have"}]},
            duration_ms=1157.0,
        ),
    )

    tool_message = history.messages()[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_8f21"
    assert tool_message.name == "grammar_check"
    assert tool_message.is_error is False
    assert json.loads(tool_message.content) == {
        "issues": [{"type": "subject_verb", "tip": "I → have"}]
    }
    _assert_every_tool_call_has_exactly_one_result(history.messages())


async def test_record_tool_result_error_still_pairs(tmp_path) -> None:
    """工具失败也是一条结果：ok=False 时把人话原因回灌给模型。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    await history.record_assistant_message(
        AssistantMessage(blocks=[ToolCallBlock(id="call_2", name="lookup", arguments={})])
    )

    await history.record_tool_result(
        "call_2",
        ToolResult(tool_name="lookup", ok=False, error="缺少已确认的原文"),
    )

    tool_message = history.messages()[-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_2"
    assert tool_message.is_error is True
    assert tool_message.content == "缺少已确认的原文"
    _assert_every_tool_call_has_exactly_one_result(history.messages())


async def test_unclosed_tool_call_is_closed_with_error_message(tmp_path) -> None:
    """调用方漏了结果：我来兜底补一条错误结果，绝不把悬空的 tool_call 发给模型。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("这一句要不要改？")
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[
                ToolCallBlock(id="call_ok", name="grammar_check", arguments={}),
                ToolCallBlock(id="call_lost", name="web_search", arguments={}),
            ]
        )
    )
    await history.record_tool_result(
        "call_ok", ToolResult(tool_name="grammar_check", ok=True, output={})
    )

    messages = history.messages()

    _assert_every_tool_call_has_exactly_one_result(messages)
    patched = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_lost"]
    assert len(patched) == 1
    assert patched[0].is_error is True
    assert patched[0].name == "web_search"
    assert patched[0].content  # 给模型一个非空的原因


async def test_turn_end_is_persisted_but_not_sent_to_model(tmp_path) -> None:
    """turn_end 是给人和排查看的终态，不进模型上下文。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    before = history.messages()

    await history.record_turn_end(
        reason=FinishReason.FINAL_TEXT,
        duration_ms=3200.5,
        usage=Usage(input_tokens=120, output_tokens=45),
        iterations=2,
    )

    assert "turn_end" in _file_types(history)
    assert history.messages() == before


async def test_reasoning_is_persisted_but_never_sent_to_model(tmp_path) -> None:
    """思考过程落盘给人看，永远不进 messages()。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    before = history.messages()

    await history.record_reasoning("先确认主语人称，再决定时态。")

    assert "reasoning" in _file_types(history)
    assert history.messages() == before
    assert "先确认主语人称" in history.path.read_text(encoding="utf-8")


async def test_cancel_is_persisted_but_not_sent_to_model(tmp_path) -> None:
    """取消也只进流水，不进上下文。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    before = history.messages()

    await history.record_cancel(reason="user", iteration=3)

    assert "cancel" in _file_types(history)
    assert history.messages() == before


async def test_compact_boundary_record_is_skipped_when_building_messages(tmp_path) -> None:
    """compact_boundary 已经被跳过了：本版没有代码产出它（这里直接写一条验证读取侧）。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("第一句")
    # 没有任何 record_* 会产出 compact_boundary，只能直接构造 —— 读取侧必须认得。
    await history._append(CompactBoundaryPayload(boundary_id="b-1", at_seq=1, summary_record_seq=1))
    await history.record_user_message("第二句")

    messages = history.messages()

    assert "compact_boundary" in _file_types(history)
    assert [type(m) for m in messages] == [SystemMessage, UserMessage, UserMessage]


async def test_messages_cache_invalidates_after_new_record(tmp_path) -> None:
    """缓存以「记录条数」为版本号：新增记录即失效；每次返回的是新列表。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("第一句")
    before = history.messages()

    await history.record_user_message("第二句")
    after = history.messages()

    assert len(after) == len(before) + 1
    assert before is not after
    assert history.messages() == after  # 重复调用结果稳定
    assert history.messages() is not after  # 每次给一份新列表，调用方别就地改


async def test_reopened_session_continues_sequence(tmp_path) -> None:
    """重开后接着原来的序号往下写，流水不会被覆盖或重排。"""
    created = await _open_new_session(tmp_path)
    await created.record_user_message("第一句")

    reopened = await open_session(
        created.session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock
    )
    assert reopened.messages() == created.messages()

    await reopened.record_user_message("第二句")

    seqs = [json.loads(line)["seq"] for line in _lines(reopened.path)]
    assert seqs == [0, 1, 2]


# ---------- 切片 5b：取消后的上下文修复 ----------


async def test_interrupted_reply_is_marked_before_next_question(tmp_path) -> None:
    """取消后用户接着问：中间补一条中断标记，上下文不能出现连续两条 user 消息。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句：I has a apple")
    await history.record_cancel(reason="user", iteration=1)
    await history.record_turn_end(reason=FinishReason.CANCELLED, duration_ms=900.0, iterations=1)

    await history.record_user_message("算了，换个题目")

    messages = history.messages(include_system=False)
    assert [type(m) for m in messages] == [UserMessage, AssistantMessage, UserMessage]
    assert messages[1].text == SYNTHETIC_TEXTS[SyntheticKind.INTERRUPTED_REPLY]
    assert messages[2].content == "算了，换个题目"


async def test_interrupt_marker_is_not_written_to_the_journal(tmp_path) -> None:
    """标记只活在 messages() 的返回值里：流水里不能多出一行 assistant_message。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    await history.record_cancel(reason="user", iteration=1)
    await history.record_user_message("算了，换个题目")

    history.messages()

    assert "assistant_message" not in _file_types(history)
    assert _file_types(history) == ["session_meta", "user_message", "cancel", "user_message"]


async def test_no_interrupt_marker_when_the_turn_just_ends(tmp_path) -> None:
    """取消后没人再说话：绝不补 —— 否则上下文会以 assistant 结尾，模型接不上。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    before = history.messages()

    await history.record_cancel(reason="user", iteration=1)
    await history.record_turn_end(reason=FinishReason.CANCELLED, duration_ms=800.0, iterations=1)

    assert history.messages() == before


async def test_repeated_cancels_produce_exactly_one_marker(tmp_path) -> None:
    """连着取消两次只补一条标记。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    await history.record_cancel(reason="user", iteration=1)
    await history.record_cancel(reason="user", iteration=2)

    await history.record_user_message("再来一次")

    messages = history.messages(include_system=False)
    assert [type(m) for m in messages] == [UserMessage, AssistantMessage, UserMessage]


async def test_tool_result_recorded_after_cancel_still_pairs_once(tmp_path) -> None:
    """取消之后才补记工具结果：一个 call_id 只能有一条结果，两条会让千问报 400。

    真实顺序是 loop 先闭合工具、再记 cancel；但反过来（工具在取消后才回报）也必须成立，
    否则「取消时先兜底闭合」与「后来到的真结果」会各留一条。
    """
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句")
    await history.record_assistant_message(
        AssistantMessage(blocks=[ToolCallBlock(id="call_1", name="grammar_check", arguments={})])
    )
    await history.record_cancel(reason="user", iteration=1)

    await history.record_tool_result(
        "call_1", ToolResult(tool_name="grammar_check", ok=False, error="已被用户取消")
    )

    messages = history.messages()
    paired = [m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == "call_1"]
    assert len(paired) == 1
    assert paired[0].content == "已被用户取消"
    _assert_every_tool_call_has_exactly_one_result(messages)


async def test_no_interrupt_marker_when_journal_starts_with_cancel(tmp_path) -> None:
    """流水万一以 cancel 开头（前面还没人说过话）：不凭空冒出一条 assistant。"""
    history = await _open_new_session(tmp_path)
    await history.record_cancel(reason="user", iteration=0)

    await history.record_user_message("第一句")

    messages = history.messages(include_system=False)
    assert [type(m) for m in messages] == [UserMessage]


async def test_interrupt_marker_survives_reopen(tmp_path) -> None:
    """写 → 关 → 打开：合成标记也算得出来，快照一字不差。"""
    created = await _open_new_session(tmp_path)
    await created.record_user_message("帮我改这句")
    await created.record_cancel(reason="user", iteration=1)
    await created.record_user_message("算了，换个题目")
    snapshot = created.messages()
    created.close()

    reopened = await open_session(
        created.session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock
    )

    assert reopened.messages() == snapshot


# ---------- 切片 5c：合成消息的身份 ----------


def test_every_synthetic_kind_has_a_text() -> None:
    """每一种身份都得配好文案，两边一一对应：加枚举忘配文案要当场炸。

    少配会补出一条空消息发给模型，多配说明有人绕开了登记表——两个方向都算错。
    """
    assert set(SyntheticKind) == set(SYNTHETIC_TEXTS)
    for kind in SyntheticKind:
        assert SYNTHETIC_TEXTS[kind].strip()


async def test_no_synthetic_text_is_ever_written_to_the_journal(tmp_path) -> None:
    """登记的每一种合成文案都不许进流水：它是现场补的视图，不是发生过的事实。

    这条测试的价值在于**可枚举** —— 将来谁新增一类合成消息，只要登记进
    ``SYNTHETIC_TEXTS``，这里就自动把它管住，不用记得回头补测试。
    前半段先证明场景确实触发了每一种，否则场景一失效它就变成永不红的假测试。
    """
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我批改这篇作文")

    # call_1 成功但 output / error 全空 → 补一句兜底（VOID_TOOL_RESULT）
    await history.record_assistant_message(
        AssistantMessage(blocks=[ToolCallBlock(id="call_1", name="grammar_check", arguments={})])
    )
    await history.record_tool_result("call_1", ToolResult(tool_name="grammar_check", ok=True))

    # call_2 发起后就取消了，此后再没等到结果 → MISSING_TOOL_RESULT
    await history.record_assistant_message(
        AssistantMessage(blocks=[ToolCallBlock(id="call_2", name="spell_check", arguments={})])
    )
    await history.record_cancel(reason="user", iteration=1)

    # 取消之后又有人提问 → INTERRUPTED_REPLY（同时也正是这一刻才给 call_2 兜底闭合）
    await history.record_user_message("再改一次")

    rendered = _rendered_text(history.messages())
    journal = history.path.read_text(encoding="utf-8")

    for kind, text in SYNTHETIC_TEXTS.items():
        assert text in rendered, f"{kind.value} 没被场景触发，这条测试就失去意义了"
        assert text not in journal, f"{kind.value} 的文案落盘了，违反「合成消息不进流水」"


# ---------- 切片 6：收尾与一致性（S6）----------


async def test_close_flushes_and_file_is_readable(tmp_path) -> None:
    """close 之后文件完整可读；再想写会被拒绝（不静默少几行）。"""
    history = await _open_new_session(tmp_path)
    await history.record_user_message("帮我改这句：I has a apple")

    history.close()

    lines = _lines(history.path)
    assert len(lines) == 2
    assert json.loads(lines[1])["type"] == "user_message"
    with pytest.raises(SessionWriteError):
        await history.record_user_message("迟到的记录")


async def test_reopened_session_produces_same_messages(tmp_path) -> None:
    """快照一致性：写 → 关 → 打开，算出来的上下文一字不差。"""
    created = await _open_new_session(tmp_path)
    await created.record_user_message("帮我改这句：I has a apple")
    await created.record_assistant_message(
        AssistantMessage(
            blocks=[
                TextBlock(text="我先查一下主谓搭配。"),
                ToolCallBlock(
                    id="call_8f21",
                    name="grammar_check",
                    arguments={"sentence": "I has a apple"},
                ),
            ]
        )
    )
    await created.record_tool_result(
        "call_8f21",
        ToolResult(
            tool_name="grammar_check",
            output={"issues": [{"type": "subject_verb", "tip": "I → have"}]},
        ),
    )
    await created.record_turn_end(reason=FinishReason.FINAL_TEXT, duration_ms=3200.5, iterations=1)
    snapshot = created.messages()
    created.close()

    reopened = await open_session(
        created.session_id, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock
    )

    assert reopened.messages() == snapshot


# ---------- 切片 7：截断续写的内部指令不落盘、不回放（Bug A）----------

_CONTINUATION_TEXT = "（上一条回复被截断，请从断点继续，不要重复已说内容）"


async def test_continuation_prompt_is_transient_and_hidden_from_replay(tmp_path) -> None:
    """输出截断让模型"接着写"的内部指令：只临时进一次上下文，不落盘、不进回放。

    回归点：旧实现把它当普通 user 消息 record_user_message 落盘，历史回放会冒出
    一个"请从断点继续"的假用户气泡。
    """
    history = await _open_new_session(tmp_path)
    await history.record_user_message("写一篇很长的作文")

    # 续写轮：临时挂上，模型要能在上下文里看见它（跟在对话记录之后）
    history.set_transient_user_message(_CONTINUATION_TEXT)
    assert _CONTINUATION_TEXT in _rendered_text(history.messages())

    # 落账即清：下一轮 assistant 入账后，临时消息不再出现在上下文
    await history.record_assistant_message(
        AssistantMessage(blocks=[TextBlock(text="（续写完成的正文）")])
    )
    assert _CONTINUATION_TEXT not in _rendered_text(history.messages())

    # 核心回归：从未落盘 —— 会话文件里没有，回放序列里也没有
    assert _CONTINUATION_TEXT not in history.path.read_text(encoding="utf-8")
    replay_text = "\n".join(item.text for item in history.replay().items)
    assert _CONTINUATION_TEXT not in replay_text


def test_session_history_signatures_match_history_port() -> None:
    """D16 的另一半：端口改了，真实现的签名不能悄悄落后。

    反向守线（loop 只准调端口方法）在 tests/runtime/test_loop.py；这条守的是
    「SessionHistory 真的实现了端口、且参数名与种类一致」——否则装配根注入真货时，
    只在续写这种冷门分支上才会炸。
    """
    port_methods = {
        name: member
        for name, member in vars(HistoryPort).items()
        if not name.startswith("_") and callable(member)
    }
    assert port_methods, "端口成员表是空的 —— 测试本身失效了"

    for name, port_method in port_methods.items():
        impl = getattr(SessionHistory, name, None)
        assert callable(impl), f"SessionHistory 没有实现端口方法 {name}"

        port_params = inspect.signature(port_method).parameters
        impl_params = inspect.signature(impl).parameters
        for pname, port_param in port_params.items():
            assert pname in impl_params, f"{name} 少了端口要求的参数 {pname}"
            assert impl_params[pname].kind == port_param.kind, (
                f"{name} 的参数 {pname} 种类不一致（端口 {port_param.kind}）"
            )


async def test_transient_message_never_appears_twice_in_one_messages_call(tmp_path) -> None:
    """临时指令在**任何一次**上下文里都只出现一条 —— 重复取不会累加成两条。

    2026-10-01 改写（原用例名 `test_transient_message_is_consumed_only_once`）：
    原版断言"第二次取就不该还在"，理由是"续写轮重发时会收到两条一模一样的指令"。
    但那件事不会发生 —— `messages()` 每次返回**新构造的列表**，临时指令只在末尾
    追加一条，不存在累加。而"看一眼就清"的副作用是真的：**重发时指令丢失**，
    模型不知道要接着写，可能重写前文。所以该守的不是"只能看一次"，
    而是"任何一次都只有一条"（重发场景见 test_transient_survives_retry_of_the_same_turn）。
    """
    history = await _open_new_session(tmp_path)
    await history.record_user_message("写一篇很长的作文")
    history.set_transient_user_message(_CONTINUATION_TEXT)

    assert _rendered_text(history.messages()).count(_CONTINUATION_TEXT) == 1
    assert _rendered_text(history.messages()).count(_CONTINUATION_TEXT) == 1


async def test_transient_survives_retry_of_the_same_turn(tmp_path) -> None:
    """续写轮若遇上流中断要重发，重发那次必须**还能**看见「请从断点继续」。

    原实现是"看一眼就清"，于是重发时这条指令已经没了：模型不知道要接着写，
    可能把前文重写一遍 —— 重发白做一轮，用户还会看到重复内容。

    2026-10-01 勘正旧理由：旧用例曾担心"不清就会收到两条一模一样的指令"。
    这个担心不成立 —— `messages()` 每次返回的是**新构造的列表**，临时指令只在
    每次返回的末尾追加一条，不存在累加。所以正确时机是"落账才清"（`_append` 已实现）。
    """
    history = await _open_new_session(tmp_path)
    await history.record_user_message("写一篇很长的作文")
    history.set_transient_user_message(_CONTINUATION_TEXT)

    first = _rendered_text(history.messages())
    second = _rendered_text(history.messages())  # 同一轮重发：再取一次上下文

    assert first.count(_CONTINUATION_TEXT) == 1
    assert second.count(_CONTINUATION_TEXT) == 1, "重发时续写指令不能丢，也不能变成两条"

    # 落账即清：本轮 assistant 入账后，指令不再出现在上下文（不污染下一轮）
    await history.record_assistant_message(
        AssistantMessage(blocks=[TextBlock(text="（续写完成的正文）")])
    )
    assert _CONTINUATION_TEXT not in _rendered_text(history.messages())


# ---------- 定稿记录进流水（v2.1 #69 / A1，方案 §8.6） ----------


async def _fresh_session(tmp_path: Path) -> SessionHistory:
    return await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model=_MODEL,
        cwd=_CWD,
        root=tmp_path,
        clock=_clock,
    )


async def test_finalize_persists_but_never_enters_messages(tmp_path: Path) -> None:
    """定稿落流水，但它不是对话：messages() 里一个字都不能出现。"""
    history = await _fresh_session(tmp_path)
    await history.record_user_message("帮我批改这篇作文")
    await history.record_finalize(
        grade_call_id="call_1",
        decisions={"d1": "accepted"},
        kept_ids=["d1"],
        comment="时态不错",
        final_text="这是改后全文XYZ",
    )

    assert history.latest_finalize_for("call_1").final_text == "这是改后全文XYZ"
    rendered = _rendered_text(history.messages())
    assert "这是改后全文XYZ" not in rendered
    assert "时态不错" not in rendered
    history.close()


async def test_latest_finalize_wins_but_older_records_remain(tmp_path: Path) -> None:
    """同一篇定稿三次：只追加、不覆盖；读取一律取最新，旧记录留在流水里可审计。"""
    history = await _fresh_session(tmp_path)
    for version in ("第一版全文", "第二版全文", "第三版全文"):
        await history.record_finalize(
            grade_call_id="call_9",
            decisions={"d1": "accepted"},
            kept_ids=["d1"],
            final_text=version,
        )
    # 另一篇的定稿不能串台
    await history.record_finalize(
        grade_call_id="call_other",
        decisions={"x": "accepted"},
        kept_ids=["x"],
        final_text="另一篇全文",
    )

    assert history.latest_finalize_for("call_9").final_text == "第三版全文"
    assert history.latest_finalize_for("call_other").final_text == "另一篇全文"
    persisted = [json.loads(line) for line in _lines(history.path)]
    assert sum(1 for r in persisted if r["type"] == "finalize") == 4
    history.close()


async def test_finalize_survives_reopen_and_latest_is_read(tmp_path: Path) -> None:
    """定稿落盘后关掉重开，仍能按 call_id 取回最新定稿版（read_artifact 的依赖）。"""
    history = await _fresh_session(tmp_path)
    sid = history.session_id
    await history.record_finalize(
        grade_call_id="call_3",
        decisions={"d1": "edited"},
        overrides={"d1": "改法"},
        edited_ids=["d1"],
        kept_ids=["d1"],
        final_text="旧版",
    )
    await history.record_finalize(
        grade_call_id="call_3",
        decisions={"d1": "accepted"},
        kept_ids=["d1"],
        final_text="新版",
    )
    history.close()

    reopened = await open_session(sid, system_prompt=_SYSTEM_PROMPT, root=tmp_path, clock=_clock)
    latest = reopened.latest_finalize_for("call_3")
    assert latest is not None
    assert latest.final_text == "新版"
    reopened.close()


async def test_latest_finalize_returns_none_when_absent(tmp_path: Path) -> None:
    history = await _fresh_session(tmp_path)
    assert history.latest_finalize_for("从未定稿的call") is None
    history.close()
