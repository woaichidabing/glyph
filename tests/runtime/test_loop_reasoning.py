"""主循环对"模型思考过程"事件的处理（阶段 2 思考采集链路）。

思考（reasoning）与正文是**两条流**：loop 只把思考增量转发给前端（前端折成思考块），
- 不进 assistant blocks、不写账本、永不进 messages()（会话层已定 reasoning 永不进模型
  视图），所以思考再长也不占上下文预算；
- 不影响终态：result.text 只含可见正文；
- 半截不入账：本轮重发时，上次尝试的思考与半截正文一起被丢弃（UI 靠 turn_retry 回退）。
"""

import asyncio
from collections.abc import AsyncIterator

from fakes import (
    FakeTurn,
    InMemoryHistory,
    ScriptedFakeStreamingLLM,
    ScriptedTool,
)

from glyph.events import AgentEvent
from glyph.llm.base import LLMError, ReasoningDelta, TextDelta, ToolCallReady, TurnEnd
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.sink import CollectingSink
from glyph.types import FinishReason, ToolCallBlock, Usage


async def collect(run: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in run]


async def test_reasoning_deltas_pass_through_without_touching_ledger() -> None:
    chunks = [
        ReasoningDelta(delta="先看看"),
        ReasoningDelta(delta="时态"),
        TextDelta(delta="答案是 42"),
        TurnEnd(stop_reason="stop", usage=Usage(input_tokens=10, output_tokens=5)),
    ]
    history = InMemoryHistory()
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(raw_events=chunks)]),
        ToolRegistry(),
        history=history,
    )
    sink = CollectingSink()

    events = await collect(loop.run("这题选什么", sink=sink))

    # 思考增量逐条按序转发，且出现在正文之前
    reasonings = [event for event in events if event.type == "reasoning_delta"]
    assert [event.delta for event in reasonings] == ["先看看", "时态"]
    kinds = [event.type for event in events]
    assert kinds.index("reasoning_delta") < kinds.index("text_delta")

    # sink（展示旁路）同样收到
    assert any(event.type == "reasoning_delta" for event in sink.events)

    # 终态只有可见正文，思考不混入 result.text
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "答案是 42"

    # 账本干净：思考不进 blocks、不进 messages()
    roles = [message.role for message in history.messages()]
    assert roles == ["system", "user", "assistant"]
    assistant = history.messages()[-1]
    assert assistant.text == "答案是 42"
    assert len(assistant.blocks) == 1  # 只有一段正文 TextBlock，思考没有块
    assert all("先看看" not in str(message) for message in history.messages())


async def test_reasoning_is_not_speaking_so_notice_still_emitted_before_tool() -> None:
    # 思考不是"说话"：模型只思考、没吐正文就调工具时，仍要发稳定的执行前提示
    # （与 WebSearchDelta 的判定一致——提示的意义是"界面上先有个说法"，思考折起不算）
    turn = FakeTurn(
        raw_events=[
            ReasoningDelta(delta="该查词典了"),
            ToolCallReady(
                call=ToolCallBlock(id="call_1", name="lookup_word", arguments={})
            ),
            TurnEnd(stop_reason="tool_calls"),
        ],
    )
    registry = ToolRegistry()
    registry.register(ScriptedTool("lookup_word", result={"ok": True}))
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([turn, FakeTurn(text="查到了")]),
        registry,
        history=InMemoryHistory(),
    )

    events = await collect(loop.run("sustain 是什么意思"))

    kinds = [event.type for event in events]
    assert kinds.count("tool_notice") == 1
    assert kinds.index("reasoning_delta") < kinds.index("tool_notice")
    assert kinds.index("tool_notice") < kinds.index("tool_call")


async def test_cancelled_half_reasoning_leaves_ledger_clean() -> None:
    # 流中途取消：思考已上屏，但没有任何 assistant 入账、result 也不含思考

    class HangingReasoningLLM:
        async def stream_chat(
            self, messages, tools, max_tokens=None, strip_tools=False, model=None
        ):
            yield ReasoningDelta(delta="想了半截")
            await asyncio.sleep(3600)
            yield TurnEnd(stop_reason="stop")  # pragma: no cover - 取消前到不了

    history = InMemoryHistory()
    loop = AgentLoop(HangingReasoningLLM(), ToolRegistry(), history=history)

    events: list[AgentEvent] = []
    async for event in loop.run("随便问问"):
        events.append(event)
        if event.type == "reasoning_delta":
            loop.cancel()

    final = events[-1]
    assert final.type == "result"
    assert final.finish_reason == FinishReason.CANCELLED
    assert not any(event.type == "assistant_message" for event in events)
    assert [message.role for message in history.messages()] == ["system", "user"]
    assert history.reasonings == []  # 半截思考同样不落盘


# ---------- 落盘：整轮收完整后整条记一次，半截尝试不记 ----------


async def test_reasoning_recorded_to_history_once_per_turn() -> None:
    chunks = [
        ReasoningDelta(delta="先看"),
        ReasoningDelta(delta="时态"),
        TextDelta(delta="答案是 42"),
        TurnEnd(stop_reason="stop"),
    ]
    history = InMemoryHistory()
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(raw_events=chunks)]),
        ToolRegistry(),
        history=history,
    )

    await collect(loop.run("这题选什么"))

    # 一轮多条思考增量拼成一条落盘（不做增量落盘）
    assert history.reasonings == ["先看时态"]


class ReasoningFailOnceLLM:
    """第一次尝试：思考后断流（可重试错误）；第二次：思考 + 完整答案。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.attempts += 1
        if self.attempts == 1:
            yield ReasoningDelta(delta="这轮不算")
            raise LLMError("连接被重置", retryable=True)
        yield ReasoningDelta(delta="重想")
        yield TextDelta(delta="稳定答案")
        yield TurnEnd(stop_reason="stop")


async def test_retried_attempt_reasoning_is_discarded_not_recorded() -> None:
    history = InMemoryHistory()
    loop = AgentLoop(
        ReasoningFailOnceLLM(),
        ToolRegistry(),
        history=history,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("问"))

    assert any(event.type == "turn_retry" for event in events)
    # 失败尝试的思考已上屏（UI 靠 turn_retry 回退），但不落盘；落盘的只有成功那次
    assert history.reasonings == ["重想"]