"""主循环对"平台内置 web_search"事件的处理（阶段 1）。

平台搜索活在千问云端：loop 只把它的三态进度转发给前端展示（🔍 搜索中 / 来源），
- 不送去 ToolRegistry 执行（它不是本地 function）；
- 不产生需要配对的工具结果、不进 assistant blocks；
- 不写进发给模型的上下文账本（阶段 1，搜索过程同思考过程，只展示）。
正文仍正常逐字流式、轮末一次性入账。切片 6 额外验证：搜索事件不破坏取消 / 断网重发的
状态一致性（半截丢弃、账本停在完整边界、重发幂等）。
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
from glyph.llm.base import LLMError, TextDelta, TurnEnd, WebSearchDelta
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.sink import CollectingSink
from glyph.types import FinishReason, Usage, WebSource


async def collect(run: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in run]


def _search_turn() -> FakeTurn:
    chunks = [
        WebSearchDelta(phase="in_progress"),
        WebSearchDelta(phase="searching", query="六级 日期"),
        WebSearchDelta(
            phase="completed",
            query="六级 日期",
            sources=[WebSource(title="NEEA", url="https://neea.example")],
        ),
        TextDelta(delta="笔试在12月12日"),
        TurnEnd(
            stop_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=5),
            web_search_count=1,
        ),
    ]
    return FakeTurn(raw_events=chunks)


# ---------- 切片 5：三态透传、不执行本地工具、不污染账本 ----------


async def test_platform_web_search_passes_through_without_local_execution() -> None:
    # 注册一个本地工具：平台搜索过程中绝不应被调用
    local_tool = ScriptedTool(
        "grade_text",
        parameters={"type": "object", "properties": {}, "required": []},
        result_fn=lambda _: {"ok": True},
    )
    registry = ToolRegistry()
    registry.register(local_tool)
    history = InMemoryHistory()
    loop = AgentLoop(ScriptedFakeStreamingLLM([_search_turn()]), registry, history=history)
    sink = CollectingSink()

    events = await collect(loop.run("查一下六级日期", sink=sink))

    # 三态搜索事件按序转发给前端
    searches = [event for event in events if event.type == "web_search"]
    assert [event.phase for event in searches] == ["in_progress", "searching", "completed"]
    assert searches[1].query == "六级 日期"
    assert searches[2].sources[0].title == "NEEA"
    assert searches[2].sources[0].url == "https://neea.example"

    # 搜索进度出现在正文之前（先看到"正在搜"，再看到答案）
    kinds = [event.type for event in events]
    assert kinds.index("web_search") < kinds.index("text_delta")

    # 本地工具一次都没执行
    assert local_tool.calls == []

    # 正文照常逐字流式 + 终态
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "笔试在12月12日"

    # 账本只有 system/user/assistant：没有工具结果，搜索过程不落盘
    roles = [message.role for message in history.messages()]
    assert roles == ["system", "user", "assistant"]
    assistant = [m for m in history.messages() if m.role == "assistant"][0]
    assert assistant.text == "笔试在12月12日"
    assert assistant.tool_calls == []
    assert all(
        getattr(message, "content", None) != "我先处理一下，再把结果告诉你。"
        for message in history.messages()
    )

    # sink（展示通道）也收到同样的搜索事件
    assert any(event.type == "web_search" for event in sink.events)


async def test_empty_web_search_turn_emits_notice_before_search() -> None:
    history = InMemoryHistory()
    loop = AgentLoop(ScriptedFakeStreamingLLM([_search_turn()]), ToolRegistry(), history=history)

    events = await collect(loop.run("查一下六级日期"))
    kinds = [event.type for event in events]

    assert kinds.count("tool_notice") == 1
    assert kinds.index("tool_notice") < kinds.index("web_search")


# ---------- 切片 6：搜索事件不破坏取消 / 断网重发 ----------


class HangingSearchLLM:
    """吐一个"搜索中"事件后长时间静默（模拟搜索 / 网络等待时用户取消）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list, list]] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append((list(messages), list(tools)))
        yield WebSearchDelta(phase="in_progress")
        await asyncio.sleep(3600)
        yield TurnEnd(stop_reason="stop")  # pragma: no cover - 取消前到不了


class SearchThenFailOnceLLM:
    """第一轮：下发搜索事件 + 半截正文后断流（可重试错误）；第二轮：给完整答案。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.attempts += 1
        if self.attempts == 1:
            yield WebSearchDelta(phase="searching", query="六级 日期")
            await asyncio.sleep(0)
            yield TextDelta(delta="半截")
            await asyncio.sleep(0)
            raise LLMError("连接被重置", retryable=True)
        yield TextDelta(delta="完整答案")
        await asyncio.sleep(0)
        yield TurnEnd(stop_reason="stop", usage=Usage(input_tokens=8, output_tokens=2))


class SearchRetryWithoutTextLLM:
    """两次尝试都先搜索；第一次断流，用来锁定重试后的提示事件。"""

    def __init__(self) -> None:
        self.attempts = 0

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.attempts += 1
        yield WebSearchDelta(phase="searching", query="四级 日期")
        if self.attempts == 1:
            raise LLMError("连接被重置", retryable=True)
        yield TextDelta(delta="查到了")
        yield TurnEnd(stop_reason="stop")


async def test_cancel_during_web_search_discards_half_and_keeps_ledger_clean() -> None:
    history = InMemoryHistory()
    loop = AgentLoop(HangingSearchLLM(), ToolRegistry(), history=history)

    events: list[AgentEvent] = []
    async for event in loop.run("查一下六级日期"):
        events.append(event)
        if event.type == "web_search":
            loop.cancel()  # 正在搜索时取消

    final = events[-1]
    assert final.type == "result"
    assert final.finish_reason == FinishReason.CANCELLED
    # 搜索进度已展示，但没有任何 assistant 消息入账
    assert any(event.type == "web_search" for event in events)
    assert not any(event.type == "assistant_message" for event in events)
    assert [m.role for m in history.messages()] == ["system", "user"]


async def test_network_drop_after_search_event_retries_and_replays_cleanly() -> None:
    history = InMemoryHistory()
    llm = SearchThenFailOnceLLM()
    loop = AgentLoop(llm, ToolRegistry(), history=history)

    events = await collect(loop.run("查一下六级日期"))

    # 发生了一次网络重发
    retries = [event for event in events if event.type == "turn_retry"]
    assert any(event.reason == "network" for event in retries)

    # 最终文本只含重发后的完整答案，失败轮的"半截"被丢弃、不进结果
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "完整答案"

    # 失败轮下发的搜索事件只出现一次（重发轮没有搜索），不重复、不残留
    searches = [event for event in events if event.type == "web_search"]
    assert len(searches) == 1

    # 账本只落了重发后那一条完整 assistant，没有半截 / 工具痕迹
    roles = [message.role for message in history.messages()]
    assert roles == ["system", "user", "assistant"]
    assert history.messages()[-1].text == "完整答案"


async def test_retry_reemits_notice_after_frontend_rolls_back_first_attempt() -> None:
    loop = AgentLoop(
        SearchRetryWithoutTextLLM(),
        ToolRegistry(),
        history=InMemoryHistory(),
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("查一下四级日期"))

    # turn_retry 会移除第一次尝试的未确认提示，第二次尝试必须重新发一条。
    assert [event.type for event in events].count("tool_notice") == 2


# ---------- 切片 8：搜索次数计入 result（成本可见） ----------


async def test_result_carries_web_search_count() -> None:
    history = InMemoryHistory()
    # _search_turn 的 TurnEnd 带 web_search_count=1
    loop = AgentLoop(ScriptedFakeStreamingLLM([_search_turn()]), ToolRegistry(), history=history)

    events = await collect(loop.run("查一下"))

    result = events[-1]
    assert result.type == "result"
    assert result.web_search_count == 1
