"""session/compaction.py 测试：上下文记账（压缩 L0 层）。

L0 **只观测、不改任何行为**——不加一条消息、不撤一个字。它是后面所有压缩动作的尺子：
没有它，去重 / 瘦身 / 摘要的阈值只能靠猜（方案 §4.6 的顺序坑就是为这个写的）。

为什么单位是**字节**不是字符：中文一个字占 3 字节，按字符算会系统性低估三分之二，
而我们要量的正是"中文为主的学习材料"。口径与 ``memory/catalog.py`` 的字节预算一致。
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from glyph.session import new_session, open_session
from glyph.session.compaction import (
    ARTIFACT_TOOLS,
    DEDUPE_MIN_BYTES,
    DEDUPE_STUB,
    SCAFFOLDING_TOOLS,
    ContextStats,
    composition_tokens,
    decide_compaction,
    dedupe,
    estimate_tokens_from_bytes,
    measure,
)
from glyph.types import (
    AssistantMessage,
    Message,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    UserMessage,
)

_TZ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 10, 3, 20, 0, 0, tzinfo=_TZ)
_SYSTEM_PROMPT = "你是一位耐心的英语助手。"
_MODEL = "qwen-plus"
_CWD = "D:\\markr"


def _clock() -> datetime:
    return _NOW


def _b(text: str) -> int:
    """一段文本在上下文里真正占的字节数。"""
    return len(text.encode("utf-8"))


def _sys(text: str) -> SystemMessage:
    return SystemMessage(content=text)


def _user(text: str) -> UserMessage:
    return UserMessage(content=text)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(blocks=[TextBlock(text=text)])


def _assistant_with_call(text: str, arguments: dict) -> AssistantMessage:
    return AssistantMessage(
        blocks=[
            TextBlock(text=text),
            ToolCallBlock(id="c1", name="grade_text", arguments=arguments),
        ]
    )


def _tool(
    name: str,
    content: str,
    *,
    is_error: bool = False,
    call_id: str = "c1",
) -> ToolMessage:
    return ToolMessage(tool_call_id=call_id, name=name, content=content, is_error=is_error)


# --------------------------------------------------------------------------
# 压缩遥测构成账：measure 七桶字节 → 逐桶保守估算 token（D2 构成，内部遥测用）


def test_composition_tokens_breaks_context_into_seven_buckets() -> None:
    # 第一条 user 按 measure 口径归 context（每轮注入块），第二条起才是 user 桶。
    messages = [
        _sys("s" * 35),  # 35B → ceil(35/3.5*1.15)=12
        _user("注入片段 " + "c" * 35),
        _user("u" * 70),  # 70B → ceil(70/3.5*1.15)=23
        _assistant_with_call("ok", {"text": "x" * 100}),  # 文本 2B；工具参数另计
        _tool("grade_text", "a" * 70),  # 成品桶 70B → 23
        _tool("lookup_word", "b" * 35),  # 脚手架桶 35B → 12
    ]
    stats = measure(messages)
    comp = composition_tokens(stats)

    # 键集合与 ContextStats.parts() 完全一致，不多不少。
    assert set(comp) == set(stats.parts())

    # 独立手算的几个桶（不调用被测函数反推期望值）。
    assert comp["system"] == 12
    assert comp["user"] == 23
    assert comp["assistant"] == 1  # "ok" 2B → ceil(2/3.5*1.15)=1
    assert comp["artifact"] == 23
    assert comp["scaffolding"] == 12
    # context 桶吃到第一条 user（注入片段），tool_call 至少含那 100 字符参数。
    assert comp["context"] > 0
    assert comp["tool_call"] >= 33

    # 成品 / 脚手架按工具名分桶，不混。
    assert comp["artifact"] > comp["scaffolding"]

    # 各桶逐桶 ceil 之和，与"总字节一次换算"只差每桶不足 1 token 的取整误差（< 桶数）。
    total_est = estimate_tokens_from_bytes(stats.total_bytes)
    assert total_est <= sum(comp.values()) <= total_est + 7


def test_composition_tokens_empty_stats_all_zero_and_params_pass_through() -> None:
    # 空称重：七桶全 0（estimate 对 0 字节返回 0）。
    empty = composition_tokens(measure([]))
    assert set(empty) == set(ContextStats().parts())
    assert all(v == 0 for v in empty.values())

    # safety / bytes_per_token 必须透传：35B、safety=1.0 → ceil(35/3.5)=10（默认 1.15 是 12）。
    comp = composition_tokens(measure([_sys("s" * 35)]), safety=1.0)
    assert comp["system"] == 10


# --------------------------------------------------------------------------
# 切片 1：基本形状
# --------------------------------------------------------------------------


def test_empty_messages_is_all_zero_and_does_not_raise() -> None:
    """空上下文：全是 0，绝不崩（消息列表为空是真实存在的开局）。"""
    stats = measure([])

    assert stats.total_bytes == 0
    assert stats.total_messages == 0
    assert set(stats.parts().values()) == {0}


def test_total_equals_sum_of_parts() -> None:
    """总计必须等于各分项之和——对不上就说明有东西漏记了。"""
    messages: list[Message] = [
        _sys("说明书"),
        _user("今天的日期：2026-10-03"),  # 每轮注入块（第一条 user）
        _user("帮我改一下这段"),
        _assistant("我先检查一下"),
        _assistant_with_call("开始批改", {"text": "I go yesterday"}),
        _tool("grade_text", '{"detections": []}'),
        _tool("lookup_word", '{"found": true}'),
    ]

    stats = measure(messages)

    assert stats.total_bytes == sum(stats.parts().values())
    assert stats.total_messages == len(messages)


def test_counts_utf8_bytes_not_characters() -> None:
    """中文按 UTF-8 字节算：3 字节/字，不是 1。按字符算会低估三分之二。"""
    messages: list[Message] = [_user("你好")]  # 2 个字符 / 6 个字节

    stats = measure(messages)

    assert stats.total_bytes == 6
    assert stats.total_bytes != 2


# --------------------------------------------------------------------------
# 切片 2：成分归属
# --------------------------------------------------------------------------


def test_system_message_counted_as_system() -> None:
    """系统词单独一桶：它是固定开销，压缩时基本动不了它。"""
    stats = measure([_sys("说明书正文"), _user("你好")])

    assert stats.system_bytes == _b("说明书正文")


def test_first_user_message_is_the_per_turn_context_slot() -> None:
    """第一条 user 是每轮注入块（日期 / 记忆目录），与 ``_build_messages`` 的顺序一致。

    为什么靠位置认：注入块不落盘、没有标记，只有组装它的人知道是哪条。
    代价写在文档字符串里——会话没注入任何片段时会把用户的第一句话算进这一桶，
    但**只影响归类，不影响总数**。
    """
    stats = measure([_sys("说明"), _user("日期与记忆目录"), _user("用户真正说的话")])

    assert stats.context_bytes == _b("日期与记忆目录")
    assert stats.user_bytes == _b("用户真正说的话")


def test_tool_call_arguments_counted_separately() -> None:
    """工具调用的参数单独一桶：复核时它会把整篇作文塞进来（方案 §3.5.2）。"""
    stats = measure([_assistant_with_call("开始", {"text": "整篇作文"})])

    assert stats.tool_call_bytes > 0
    assert stats.assistant_bytes == _b("开始")


# --------------------------------------------------------------------------
# 切片 3：工具结果的两类归属（这是压缩时唯一真正要区分的东西）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(ARTIFACT_TOOLS))
def test_artifact_tools_go_to_artifact_bucket(tool_name: str) -> None:
    """成品类：结果会被翻出来追问，压的时候要挑着压。"""
    stats = measure([_tool(tool_name, '{"a": 1}')])

    assert stats.artifact_bytes == _b('{"a": 1}')
    assert stats.scaffolding_bytes == 0


@pytest.mark.parametrize("tool_name", sorted(SCAFFOLDING_TOOLS))
def test_scaffolding_tools_go_to_scaffolding_bucket(tool_name: str) -> None:
    """脚手架类：用完即弃，过几轮就能整条撤掉。"""
    stats = measure([_tool(tool_name, '{"a": 1}')])

    assert stats.scaffolding_bytes == _b('{"a": 1}')
    assert stats.artifact_bytes == 0


def test_unknown_tool_defaults_to_artifact() -> None:
    """不认识的工具按**成品**处理——保守默认。

    为什么保守：将来加新工具的人多半不会回来更新这个名单。归成品 = 不敢乱压，
    最坏只是没压；归脚手架 = 可能把用户正要追问的东西撤了。
    """
    stats = measure([_tool("某个将来新加的工具", '{"a": 1}')])

    assert stats.artifact_bytes == _b('{"a": 1}')
    assert stats.scaffolding_bytes == 0


def test_error_results_are_counted_too() -> None:
    """报错回灌也占地方，而且要看得见——它是"白占的那一块"。"""
    stats = measure([_tool("grade_text", "次数已用完", is_error=True)])

    assert stats.artifact_bytes == _b("次数已用完")


@pytest.mark.parametrize(
    "arguments",
    [
        {"when": datetime(2026, 10, 3)},  # 日期：JSON 不认的类型
        {"self": {}},  # 下面会塞成循环引用：JSON 直接报循环引用
    ],
)
def test_unserializable_arguments_do_not_break_measurement(arguments: dict) -> None:
    """参数是模型给的，可能塞进 JSON 不认的东西——记账不许因此崩。

    记一笔账是**只读观测**，它把整轮搞挂是最不可接受的结果：
    宁可把参数降级成字符串量个大概，也不能抛异常。

    两个用例分别守住兜底的两条入口：``TypeError``（不认的类型）与 ``ValueError``
    （循环引用）。变异检验过：去掉 try/except 这两条都会红。
    """
    arguments["self"] = arguments  # 自引用：json.dumps 会抛循环引用
    message = AssistantMessage(
        blocks=[ToolCallBlock(id="c1", name="grade_text", arguments=arguments)]
    )

    stats = measure([message])

    assert stats.tool_call_bytes > 0


# --------------------------------------------------------------------------
# 切片 4：稳定性
# --------------------------------------------------------------------------


def test_measure_is_deterministic() -> None:
    """同样的输入必须算出同样的结果——记账本身不能有随机性。"""
    messages: list[Message] = [_sys("说明"), _user("你好"), _tool("grade_text", "{}")]

    assert measure(messages) == measure(messages)


def test_stats_is_frozen() -> None:
    """统计结果是只读的：谁都不许就地改它。"""
    stats = measure([_user("你好")])

    with pytest.raises(FrozenInstanceError):
        stats.total_bytes = 1  # type: ignore[misc]


# --------------------------------------------------------------------------
# 切片 5：接到真实会话上
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_reports_stats_for_its_own_messages(tmp_path: Path) -> None:
    """端到端：会话自己报的数，必须和它实际发出的消息对得上。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model=_MODEL,
        cwd=_CWD,
        root=tmp_path,
        clock=_clock,
    )
    await history.record_user_message("帮我改一下这段英文")
    await history.record_assistant_message(_assistant("我先检查一下"))
    await history.record_tool_result(
        "c1", ToolResult(tool_name="grade_text", ok=True, output={"detections": []})
    )

    stats = history.context_stats()

    assert stats.total_bytes == sum(stats.parts().values())
    assert stats.total_bytes > 0
    assert stats.system_bytes == _b(_SYSTEM_PROMPT)
    assert stats.artifact_bytes > 0
    history.close()


@pytest.mark.asyncio
async def test_stats_do_not_change_messages(tmp_path: Path) -> None:
    """记账是只读的：问一次数，上下文一个字都不能变。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model=_MODEL,
        cwd=_CWD,
        root=tmp_path,
        clock=_clock,
    )
    await history.record_user_message("你好")
    before = history.messages()

    history.context_stats()

    assert history.messages() == before
    history.close()


# --------------------------------------------------------------------------
# 切片 6：L1 去重 —— 同一份材料只留最近一份，前面几次降为一行存根
#
# 方案 §3.5.2：膨胀的主因不是"历史越来越长"，而是同一份材料被反复回灌。
# 这一层零损失（重复就是重复），所以排在所有刀法前面。
# --------------------------------------------------------------------------

_ESSAY = (
    "I go to the park yesterday and I see many peoples there. "
    "They was playing football when I arrived at there. "
    "I think it is a very good day for me and my friends. "
) * 3


def _recheck(text: Any, *, call_id: str = "c1") -> AssistantMessage:
    """一条复核调用：整篇原文 + 一条 detection（真实 recheck_detection 的入参形态）。"""
    return AssistantMessage(
        blocks=[
            ToolCallBlock(
                id=call_id,
                name="recheck_detection",
                arguments={"text": text, "detection": {"id": "d1"}, "round": 1},
            )
        ]
    )


def _grade(text: str, *, call_id: str = "c1") -> AssistantMessage:
    """一条批改调用：定稿接口要用它的真实入参，永远不许被去重（§3.5.4 硬约束）。"""
    return AssistantMessage(
        blocks=[ToolCallBlock(id=call_id, name="grade_text", arguments={"text": text})]
    )


def _arg_texts(messages: list[Message], tool_name: str) -> list[Any]:
    """取某个工具所有调用的 text 入参，按出现顺序（缺字段即 None）。"""
    out: list[Any] = []
    for m in messages:
        if isinstance(m, AssistantMessage):
            for call in m.tool_calls:
                if call.name == tool_name:
                    out.append(call.arguments.get("text"))
    return out


def test_dedupe_leaves_a_single_occurrence_untouched() -> None:
    """只发过一次的原文：没有重复可去，一个字都不许动。"""
    messages: list[Message] = [_recheck(_ESSAY)]

    assert _arg_texts(dedupe(messages), "recheck_detection") == [_ESSAY]


def test_dedupe_keeps_only_the_latest_copy() -> None:
    """两次复核同一篇：最近一次留原文，前面那次降为存根。"""
    messages: list[Message] = [
        _recheck(_ESSAY, call_id="c1"),
        _recheck(_ESSAY, call_id="c2"),
    ]

    after = _arg_texts(dedupe(messages), "recheck_detection")

    assert after[0] == DEDUPE_STUB
    assert after[1] == _ESSAY


def test_dedupe_collapses_every_earlier_copy() -> None:
    """三次复核同一篇（配额一篇最多 5 次）：只留最后一次，前面全部降为存根。"""
    messages: list[Message] = [_recheck(_ESSAY, call_id=f"c{i}") for i in range(1, 4)]

    assert _arg_texts(dedupe(messages), "recheck_detection") == [
        DEDUPE_STUB,
        DEDUPE_STUB,
        _ESSAY,
    ]


def test_dedupe_ignores_different_text() -> None:
    """原文改过了就不是重复：两版都得留着，否则模型会以为后一次复核的是旧版。"""
    revised = _ESSAY + "And I am very happy about it. "
    messages: list[Message] = [
        _recheck(_ESSAY, call_id="c1"),
        _recheck(revised, call_id="c2"),
    ]

    after = _arg_texts(dedupe(messages), "recheck_detection")

    assert after == [_ESSAY, revised]


def test_dedupe_never_touches_grade_text() -> None:
    """硬约束：grade_text 的 text 不许去重 —— 定稿要用当次的真实入参（§3.5.4）。"""
    messages: list[Message] = [
        _grade(_ESSAY, call_id="c1"),
        _grade(_ESSAY, call_id="c2"),
    ]

    assert _arg_texts(dedupe(messages), "grade_text") == [_ESSAY, _ESSAY]


def test_dedupe_preserves_count_order_and_call_ids() -> None:
    """去重只换内容、不动结构：条数、顺序、call_id 全得原样（前端裁决靠 call_id 对号）。"""
    messages: list[Message] = [
        _user("帮我改"),
        _recheck(_ESSAY, call_id="c1"),
        _tool("recheck_detection", '{"status": "upheld"}', call_id="c1"),
        _recheck(_ESSAY, call_id="c2"),
        _tool("recheck_detection", '{"status": "revised"}', call_id="c2"),
    ]

    after = dedupe(messages)

    assert len(after) == len(messages)
    assert [type(m) for m in after] == [type(m) for m in messages]
    assert [c.id for m in after if isinstance(m, AssistantMessage) for c in m.tool_calls] == [
        "c1",
        "c2",
    ]
    assert [m.tool_call_id for m in after if isinstance(m, ToolMessage)] == ["c1", "c2"]


def test_dedupe_shrinks_the_context() -> None:
    """去重的目的就是变小：省下多少用 L0 那把尺子量，必须正好是两份原文的差。"""
    messages: list[Message] = [_recheck(_ESSAY, call_id=f"c{i}") for i in range(1, 4)]

    before = measure(messages)
    after = measure(dedupe(messages))

    saved = before.total_bytes - after.total_bytes
    assert saved == 2 * (_b(_ESSAY) - _b(DEDUPE_STUB))


def test_dedupe_skips_values_shorter_than_the_stub() -> None:
    """太短的值不去重：存根比原文还长，去它只会让上下文变大。"""
    short = "I go yesterday"
    assert _b(short) < DEDUPE_MIN_BYTES  # 前提：确实够短，够不上去重门槛

    messages: list[Message] = [
        _recheck(short, call_id="c1"),
        _recheck(short, call_id="c2"),
    ]

    assert _arg_texts(dedupe(messages), "recheck_detection") == [short, short]


def test_dedupe_does_not_mutate_its_input() -> None:
    """纯函数：入参一个字都不许改。``messages()`` 有缓存，就地改会污染缓存里的对象。"""
    messages: list[Message] = [
        _recheck(_ESSAY, call_id="c1"),
        _recheck(_ESSAY, call_id="c2"),
    ]
    snapshot = [m.model_dump() for m in messages]

    dedupe(messages)

    assert [m.model_dump() for m in messages] == snapshot
    assert _arg_texts(messages, "recheck_detection") == [_ESSAY, _ESSAY]


def test_dedupe_is_idempotent() -> None:
    """压一次和压两次必须一样：流中断整轮重发时会连着调多次（§6.2）。"""
    messages: list[Message] = [
        _recheck(_ESSAY, call_id="c1"),
        _recheck(_ESSAY, call_id="c2"),
    ]

    once = dedupe(messages)

    assert [m.model_dump() for m in dedupe(once)] == [m.model_dump() for m in once]


def test_dedupe_tolerates_missing_and_non_string_values() -> None:
    """模型给的参数什么形态都有：缺字段、给了 dict、给了 None，一律不许崩。"""
    messages: list[Message] = [
        AssistantMessage(blocks=[ToolCallBlock(id="c1", name="recheck_detection", arguments={})]),
        AssistantMessage(
            blocks=[
                ToolCallBlock(
                    id="c2",
                    name="recheck_detection",
                    arguments={"text": {"id": "d1"}, "detection": 123},
                )
            ]
        ),
        AssistantMessage(
            blocks=[ToolCallBlock(id="c3", name="recheck_detection", arguments={"text": None})]
        ),
    ]

    texts = _arg_texts(dedupe(messages), "recheck_detection")

    assert texts[0] is None
    assert texts[1] == {"id": "d1"}
    assert texts[2] is None


def test_empty_messages_dedupe_to_empty() -> None:
    """空上下文：去重也得返回空，不能凭空造消息。"""
    assert dedupe([]) == []


def test_dedupe_handles_several_calls_in_one_message() -> None:
    """一轮里连发两条复核（并行工具调用）：按**调用**逐个判，不是按消息判。"""
    other = _ESSAY + "And I am very happy about it. "
    messages: list[Message] = [
        AssistantMessage(
            blocks=[
                ToolCallBlock(id="c1", name="recheck_detection", arguments={"text": _ESSAY}),
                ToolCallBlock(id="c2", name="recheck_detection", arguments={"text": other}),
            ]
        ),
        _recheck(_ESSAY, call_id="c3"),
    ]

    after = _arg_texts(dedupe(messages), "recheck_detection")

    # c1 与 c3 是同一份原文 → c1 降存根、c3 留原文；c2 是另一份，不动
    assert after == [DEDUPE_STUB, other, _ESSAY]


# --------------------------------------------------------------------------
# 切片 7：接进会话（去重真的在发给模型之前发生）
# --------------------------------------------------------------------------


async def _session_with_two_rechecks(tmp_path: Path, **kwargs: Any) -> Any:
    """一个连着两次复核同一篇作文的真实会话（方案 §3.5.2 说的就是它）。"""
    history = await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model=_MODEL,
        cwd=_CWD,
        root=tmp_path,
        clock=_clock,
        **kwargs,
    )
    for call_id in ("c1", "c2"):
        await history.record_assistant_message(_recheck(_ESSAY, call_id=call_id))
        await history.record_tool_result(
            call_id,
            ToolResult(tool_name="recheck_detection", ok=True, output={"status": "upheld"}),
        )
    return history


@pytest.mark.asyncio
async def test_history_drops_the_duplicated_recheck_text(tmp_path: Path) -> None:
    """端到端：连着复核同一篇两次，发给模型的上下文里只留最近一份原文。"""
    history = await _session_with_two_rechecks(tmp_path)

    texts = _arg_texts(history.messages(), "recheck_detection")

    assert texts == [DEDUPE_STUB, _ESSAY]
    history.close()


@pytest.mark.asyncio
async def test_dedupe_can_be_switched_off(tmp_path: Path) -> None:
    """开关关掉就退回原样：压缩层真出问题时，这是唯一的退路。"""
    history = await _session_with_two_rechecks(tmp_path, context_dedupe=False)

    assert _arg_texts(history.messages(), "recheck_detection") == [_ESSAY, _ESSAY]
    history.close()


@pytest.mark.asyncio
async def test_messages_are_stable_across_repeated_calls(tmp_path: Path) -> None:
    """连着问两次必须给同样的答案：流中断整轮重发时会连着调，不能一次一个样。"""
    history = await _session_with_two_rechecks(tmp_path)

    first = [m.model_dump() for m in history.messages()]
    second = [m.model_dump() for m in history.messages()]

    assert second == first
    history.close()


@pytest.mark.asyncio
async def test_dedupe_does_not_touch_the_recorded_flow(tmp_path: Path) -> None:
    """去重只改"发给模型的那一叠"，落盘流水一个字没动 —— 压缩永远可回滚。"""
    history = await _session_with_two_rechecks(tmp_path)
    history.messages()  # 触发一次去重
    history.close()

    reopened = await open_session(
        history.session_id,
        system_prompt=_SYSTEM_PROMPT,
        root=tmp_path,
        clock=_clock,
        context_dedupe=False,  # 关掉去重，看流水里存的到底是什么
    )

    assert _arg_texts(reopened.messages(), "recheck_detection") == [_ESSAY, _ESSAY]
    reopened.close()


# ---------- 决策层：字节 → token 保守估算（v2.1 #62，方案 §6.1） ----------


def test_estimate_tokens_uses_conservative_bytes_per_token() -> None:
    """350 字节按 3.5 字节/token 是 100 token；中文偏多，再上浮安全边际后更多。"""
    raw = math.ceil(350 / 3.5)
    assert estimate_tokens_from_bytes(350) > raw
    # 安全边际默认 1.15：100 × 1.15 = 115
    assert estimate_tokens_from_bytes(350) == 115


def test_estimate_tokens_rounds_up_and_handles_zero() -> None:
    assert estimate_tokens_from_bytes(0) == 0
    # 360 / 3.5 = 102.86，× 1.15 = 118.29 → 向上取整 119（宁可多估，不可少估）
    assert estimate_tokens_from_bytes(360) == 119


def test_estimate_tokens_never_below_raw_division() -> None:
    """安全边际的意义：任意字节量下估算都不小于裸除值（宁早压、不晚压）。"""
    for n in (1, 7, 100, 10_000, 1_000_000):
        assert estimate_tokens_from_bytes(n) >= math.ceil(n / 3.5)


# ---------- 决策层：三态决策器（v2.1 #62，方案 §6.1 / §6.4） ----------
#
# 标准档 W=300_000 下三条线（测试里直接写死字面量，不引用实现常量，避免同义反复）：
#   L2 线   = 300_000 × 0.50 = 150_000
#   L3 线   = 300_000 − 90_000 = 210_000
#   硬阻断  = 300_000 − 12_000 = 288_000


def test_decide_normal_when_well_below_l2_line() -> None:
    """水位低：不压。预判水位 = 上轮 input + 上轮 output + 本轮新增（§6.1）。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=100_000,
        last_output_tokens=2_000,
        estimated_tokens=100_000,
        new_user_estimated_tokens=500,
    )
    assert d.state == "normal"
    assert d.levels == ()
    assert d.predicted_tokens == 102_500
    assert d.window_tokens == 300_000


def test_decide_first_turn_without_usage_only_l1_even_if_estimate_high() -> None:
    """首轮 / 拿不到真实 input_tokens：没尺子，只做 L1，不主动压也不主动阻断。

    本地估算哪怕已过硬线也返回 normal——首轮真撞 413 走被动应急（§10），
    不由决策器拍板。水位仍照算填进结果，供观测。
    """
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=None,
        last_output_tokens=None,
        estimated_tokens=290_000,
    )
    assert d.state == "normal"
    assert d.levels == ()
    assert d.predicted_tokens == 290_000


def test_decide_l2_when_past_l2_line_only() -> None:
    """过 L2 线（150_000）但没到 L3 线（210_000）：compact，只做免费的 L2 装箱。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=159_000,
        last_output_tokens=1_000,
        estimated_tokens=160_000,
    )
    assert d.predicted_tokens == 160_000
    assert d.state == "compact"
    assert d.levels == ("l2",)


def test_decide_l2_and_l3_when_past_compact_line() -> None:
    """过 L3 线（210_000）：L2 装箱照做，再花钱调一次摘要，levels 两个都带。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=214_000,
        last_output_tokens=1_000,
        estimated_tokens=215_000,
    )
    assert d.predicted_tokens == 215_000
    assert d.state == "compact"
    assert d.levels == ("l2", "l3")


def test_decide_blocked_when_past_hard_line() -> None:
    """过硬阻断线（288_000）：blocked，本轮发送前必须先应急减重，levels 不再带压缩档。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=288_500,
        last_output_tokens=1_000,
        estimated_tokens=289_500,
    )
    assert d.predicted_tokens == 289_500
    assert d.state == "blocked"
    assert d.levels == ()


def test_decide_a2_output_feedback_can_push_compact_into_blocked() -> None:
    """A2 核心回归：上轮输出下一轮全额回灌成输入。

    同样上轮 input=225_000：单看它只过 L3 线（210k）、没过硬线（288k）；
    但若上轮升档输出了 65_536，回灌后 290_536 直接过硬阻断线。
    漏算 output 这笔，就会把"必须应急"误判成"做个摘要就行"。
    """
    common = dict(
        effective_window=300_000,
        last_input_tokens=225_000,
        estimated_tokens=225_000,
    )
    without_output = decide_compaction(last_output_tokens=0, **common)
    assert without_output.state == "compact"
    assert without_output.levels == ("l2", "l3")

    with_big_output = decide_compaction(last_output_tokens=65_536, **common)
    assert with_big_output.predicted_tokens == 290_536
    assert with_big_output.state == "blocked"


def test_decide_l2_sticks_once_activated_even_when_level_drops() -> None:
    """水位已跌回 L2 线（150k）以下，但本会话 L2 已启用：保持装箱，不反复横跳。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=119_000,
        last_output_tokens=1_000,
        estimated_tokens=120_000,
        l2_active=True,
    )
    assert d.state == "compact"
    assert d.levels == ("l2",)


def test_decide_no_l2_below_line_when_never_activated() -> None:
    """对照组：同样 120k 水位、从没启用过 L2 → normal（粘滞只向一个方向粘）。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=119_000,
        last_output_tokens=1_000,
        estimated_tokens=120_000,
        l2_active=False,
    )
    assert d.state == "normal"


def test_decide_switch_off_skips_active_levels_but_keeps_hard_block() -> None:
    """C3：总开关只关主动 L2/L3；过硬阻断线时应急保命照样触发。"""
    over_l3 = decide_compaction(
        effective_window=300_000,
        last_input_tokens=220_000,
        last_output_tokens=0,
        estimated_tokens=220_000,
        auto_compact=False,
    )
    assert over_l3.state == "normal"
    assert over_l3.levels == ()

    over_hard = decide_compaction(
        effective_window=300_000,
        last_input_tokens=290_000,
        last_output_tokens=0,
        estimated_tokens=290_000,
        auto_compact=False,
    )
    assert over_hard.state == "blocked"


def test_decide_thresholds_follow_effective_window() -> None:
    """同一水位 250k：标准档（W=300k）已过 L3 线；Max 档（W=1M，L2 线 500k）正常。

    切档位 W 变化，所有阈值必须跟着 W 重算，不能写死成标准档的绝对值。
    """
    std = decide_compaction(
        effective_window=300_000,
        last_input_tokens=250_000,
        last_output_tokens=0,
        estimated_tokens=250_000,
    )
    assert std.state == "compact"
    assert std.levels == ("l2", "l3")

    max_window = decide_compaction(
        effective_window=1_000_000,
        last_input_tokens=250_000,
        last_output_tokens=0,
        estimated_tokens=250_000,
    )
    assert max_window.state == "normal"


def test_decide_boundary_values_are_inclusive() -> None:
    """恰好压在线上按"到线"处理（>=）；差一个 token 都不压。"""

    def at(predicted: int):
        return decide_compaction(
            effective_window=300_000,
            last_input_tokens=predicted,
            last_output_tokens=0,
            estimated_tokens=predicted,
        )

    assert at(149_999).state == "normal"
    assert at(150_000).levels == ("l2",)
    assert at(210_000).levels == ("l2", "l3")
    assert at(288_000).state == "blocked"


def test_decide_missing_output_counted_as_zero() -> None:
    """有真实 input 但这轮没回 output（异常 / 缺失）：按 0 记，不报错也不造数。"""
    d = decide_compaction(
        effective_window=300_000,
        last_input_tokens=100_000,
        last_output_tokens=None,
        estimated_tokens=100_000,
        new_user_estimated_tokens=300,
    )
    assert d.predicted_tokens == 100_300
    assert d.state == "normal"
