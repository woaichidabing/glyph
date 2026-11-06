"""AgentLoop 测试。

账本不再由 loop 自持，而是构造注入的会话历史（HistoryPort）：
- 离线测试用 ``InMemoryHistory``（不写盘）；
- 断言"这一轮发给模型的上下文"一律看 ``history.messages()``；
- 一次 run 收尾除了发 result 事件，还必须落 ``record_turn_end``；取消额外落 ``record_cancel``。
"""

import asyncio
import json
import logging
import pathlib
import re
import time
from collections.abc import AsyncIterator

from fakes import (
    FailingStreamingLLM,
    FakeTurn,
    FlakyStreamingLLM,
    GateTool,
    HangingStreamingLLM,
    HangingTool,
    InMemoryHistory,
    InteractiveGateTool,
    ScriptedFakeStreamingLLM,
    ScriptedTool,
    SlowStreamingLLM,
    StalledStreamingLLM,
    StubbornTool,
)

from glyph.context import CONTINUATION_PROMPT
from glyph.events import AgentEvent
from glyph.llm.base import LLMError
from glyph.llm.models import context_window_of
from glyph.runtime import loop as loop_module
from glyph.runtime.errors import ToolError
from glyph.runtime.history_port import HistoryPort
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.sink import CollectingSink
from glyph.session.compaction_l3 import CompactionPlan, CompactionReport
from glyph.session.errors import SessionWriteError
from glyph.tools.base import ReadOnlyTool, WriteTool
from glyph.types import AssistantMessage, FinishReason, Message, ToolResult, Usage


async def collect(run: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in run]


def make_loop(turns, registry=None) -> AgentLoop:
    history = InMemoryHistory()
    return AgentLoop(ScriptedFakeStreamingLLM(turns), registry or ToolRegistry(), history=history)


async def test_plain_text_reply_streams_deltas_and_commits_one_message() -> None:
    loop = make_loop([FakeTurn(text="你好，我是助手")])
    sink = CollectingSink()

    events = await collect(loop.run("嗨", sink=sink))
    kinds = [e.type for e in events]

    # 起止事件
    assert kinds[0] == "run_started"
    assert kinds[-1] == "result"

    # 逐字流式：多片 text_delta，拼起来等于整句
    delta_events = [e for e in events if e.type == "text_delta"]
    assert len(delta_events) > 1
    assert "".join(e.delta for e in delta_events) == "你好，我是助手"

    # 完整 assistant 消息只入账一次（delta 不重复进账本）
    msg_events = [e for e in events if e.type == "assistant_message"]
    assert len(msg_events) == 1
    assert msg_events[0].message.text == "你好，我是助手"

    # 账本顺序：system + user + assistant（由注入的 history 保管）
    assert [m.role for m in loop.history.messages()] == ["system", "user", "assistant"]

    # sink 收到与 yield 完全一致的事件流（事件分流 seam）
    assert [e.type for e in sink.events] == kinds

    # 终态
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.iterations == 1
    assert result.text == "你好，我是助手"


async def test_single_tool_result_is_fed_back_by_id() -> None:
    echo = ScriptedTool(
        "echo",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        result_fn=lambda a: {"echoed": a["text"]},
    )
    registry = ToolRegistry()
    registry.register(echo)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="我先调用回声", tool_calls=[("echo", {"text": "hi"})]),
            FakeTurn(text="工具返回了 hi"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)

    events = await collect(loop.run("请回声 hi"))
    kinds = [e.type for e in events]

    # 轮边界后：先宣告工具、再给结果
    call_events = [e for e in events if e.type == "tool_call"]
    result_events = [e for e in events if e.type == "tool_result"]
    assert len(call_events) == 1 and len(result_events) == 1
    assert kinds.index("tool_call") < kinds.index("tool_result")
    assert kinds.index("assistant_message") < kinds.index("tool_call")

    call_event = call_events[0]
    result_event = result_events[0]
    # 结果与调用按 id 严格配对
    assert call_event.call.id == "call_1_0"
    assert result_event.call_id == "call_1_0"
    assert result_event.name == "echo"
    assert result_event.ok is True
    assert result_event.output == {"echoed": "hi"}
    assert result_event.duration_ms >= 0

    # 第二次请求模型时，messages 里已有与 id 配对的 tool 结果
    second_messages = llm.calls[1][0]
    tool_msgs = [m for m in second_messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_1_0"
    assert tool_msgs[0].is_error is False
    assert json.loads(tool_msgs[0].content) == {"echoed": "hi"}

    # 账本：system / user / assistant / tool / assistant
    assert [m.role for m in history.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]

    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.iterations == 2


async def test_empty_tool_turn_emits_notice_before_tool_call() -> None:
    tool = ScriptedTool(
        "echo",
        parameters={"type": "object", "properties": {}, "required": []},
        result={"ok": True},
    )
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        ScriptedFakeStreamingLLM(
            [
                FakeTurn(tool_calls=[("echo", {})]),
                FakeTurn(text="处理好了"),
            ]
        ),
        registry,
        history=InMemoryHistory(),
    )

    events = await collect(loop.run("帮我处理一下"))
    kinds = [event.type for event in events]

    assert kinds.count("tool_notice") == 1
    assert kinds.index("tool_notice") < kinds.index("tool_call")
    assert events[kinds.index("tool_notice")].message == "我先处理一下，再把结果告诉你。"


async def test_text_before_tool_does_not_get_synthetic_notice() -> None:
    tool = ScriptedTool(
        "echo",
        parameters={"type": "object", "properties": {}, "required": []},
        result={"ok": True},
    )
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        ScriptedFakeStreamingLLM(
            [
                FakeTurn(text="我先处理一下", tool_calls=[("echo", {})]),
                FakeTurn(text="处理好了"),
            ]
        ),
        registry,
        history=InMemoryHistory(),
    )

    events = await collect(loop.run("帮我处理一下"))

    assert not any(event.type == "tool_notice" for event in events)


async def test_whitespace_before_tool_still_emits_notice() -> None:
    tool = ScriptedTool(
        "echo",
        parameters={"type": "object", "properties": {}, "required": []},
        result={"ok": True},
    )
    registry = ToolRegistry()
    registry.register(tool)
    loop = AgentLoop(
        ScriptedFakeStreamingLLM(
            [
                FakeTurn(text="\n", tool_calls=[("echo", {})]),
                FakeTurn(text="处理好了"),
            ]
        ),
        registry,
        history=InMemoryHistory(),
    )

    events = await collect(loop.run("帮我处理一下"))

    kinds = [event.type for event in events]
    assert kinds.count("tool_notice") == 1
    assert kinds.index("tool_notice") < kinds.index("tool_call")


async def test_model_recovers_after_tool_error_by_trying_another_tool() -> None:
    flaky = ScriptedTool("flaky", raises=ToolError("暂时不可用"))
    good = ScriptedTool("good", result={"ok": True})
    registry = ToolRegistry()
    registry.register(flaky)
    registry.register(good)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("flaky", {})]),
            FakeTurn(tool_calls=[("good", {})]),
            FakeTurn(text="换用 good 后完成"),
        ]
    )
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    events = await collect(loop.run("试试"))
    results = [e for e in events if e.type == "tool_result"]

    assert len(results) == 2
    assert results[0].name == "flaky" and results[0].ok is False
    assert "暂时不可用" in results[0].error
    assert results[1].name == "good" and results[1].ok is True

    # 第三次请求前，账本含"一错一对"两条工具结果
    tool_msgs = [m for m in llm.calls[2][0] if m.role == "tool"]
    assert [m.name for m in tool_msgs] == ["flaky", "good"]
    assert [m.is_error for m in tool_msgs] == [True, False]

    final = events[-1]
    assert final.finish_reason == FinishReason.FINAL_TEXT
    assert final.iterations == 3


async def test_hallucinated_tool_name_fed_back_then_model_uses_real_one() -> None:
    real = ScriptedTool("real", result={"done": True})
    registry = ToolRegistry()
    registry.register(real)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("imagined_tool", {})]),
            FakeTurn(tool_calls=[("real", {})]),
            FakeTurn(text="改用真实工具完成"),
        ]
    )
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    events = await collect(loop.run("x"))
    results = [e for e in events if e.type == "tool_result"]

    # 幻觉工具名：回一条带"可用清单"的错误，而不是炸循环
    assert results[0].name == "imagined_tool" and results[0].ok is False
    assert "imagined_tool" in results[0].error
    assert "real" in results[0].error
    # 模型读懂后改用真实工具
    assert results[1].name == "real" and results[1].ok is True

    final = events[-1]
    assert final.finish_reason == FinishReason.FINAL_TEXT
    assert final.iterations == 3


async def test_multiple_tools_run_in_parallel_and_all_settle() -> None:
    # 两个各耗时 0.08s 的工具 + 一个必失败的工具
    tool_a = ScriptedTool("ok_a", result={"v": "a"}, delay=0.08)
    tool_b = ScriptedTool("ok_b", result={"v": "b"}, delay=0.08)
    tool_c = ScriptedTool("boom_c", raises=ToolError("c 做不了"))
    registry = ToolRegistry()
    for tool in (tool_a, tool_b, tool_c):
        registry.register(tool)

    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("ok_a", {}), ("ok_b", {}), ("boom_c", {})]),
            FakeTurn(text="都处理完了"),
        ]
    )
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    start = time.perf_counter()
    events = await collect(loop.run("并行跑三个"))
    elapsed = time.perf_counter() - start

    results = {e.call_id: e for e in events if e.type == "tool_result"}
    assert set(results) == {"call_1_0", "call_1_1", "call_1_2"}
    by_name = {e.name: e for e in results.values()}
    assert by_name["ok_a"].ok is True and by_name["ok_b"].ok is True
    # 一个失败不拖累同轮其他工具（allSettled）
    assert by_name["boom_c"].ok is False
    assert "c 做不了" in by_name["boom_c"].error

    # 并行：两个 0.08s 的工具应重叠完成（串行会是 ~0.16s）
    assert elapsed < 0.13

    # 三条结果（含错误）都回灌，模型据此收尾
    tool_msgs = [m for m in llm.calls[1][0] if m.role == "tool"]
    assert len(tool_msgs) == 3
    assert sum(1 for m in tool_msgs if m.is_error) == 1

    final = events[-1]
    assert final.finish_reason == FinishReason.FINAL_TEXT
    assert final.iterations == 2


async def test_consecutive_tool_failures_reach_threshold_then_failed() -> None:
    bad = ScriptedTool("bad", raises=ToolError("又失败了"))
    registry = ToolRegistry()
    registry.register(bad)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("bad", {})]),
            FakeTurn(tool_calls=[("bad", {})]),
            FakeTurn(tool_calls=[("bad", {})]),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)  # 默认连续失败阈值 3

    events = await collect(loop.run("跑"))
    final = events[-1]

    assert final.type == "result"
    assert final.finish_reason == FinishReason.FAILED
    assert final.iterations == 3
    # 达到阈值后不再请求模型（剧本正好 3 轮，多调会在假模型处报错）
    assert len(llm.calls) == 3
    # 三次失败结果都已闭合回灌，没有悬空 tool_call
    failed_results = [e for e in events if e.type == "tool_result" and not e.ok]
    assert len(failed_results) == 3
    # 账本里三条 tool 消息都是错误
    assert len([m for m in history.messages() if m.role == "tool" and m.is_error]) == 3


async def test_max_iterations_then_forces_summary_without_tools() -> None:
    good = ScriptedTool("good", result={"v": 1})
    registry = ToolRegistry()
    registry.register(good)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("good", {})]),
            FakeTurn(tool_calls=[("good", {})]),
            FakeTurn(text="这是最终总结"),  # 第 3 次请求：强制总结，不带工具
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history, max_iterations=2)

    events = await collect(loop.run("一直调工具"))
    final = events[-1]

    assert final.finish_reason == FinishReason.MAX_ITERATIONS
    assert final.iterations == 2
    assert final.text == "这是最终总结"

    # 前两次请求带工具，强制总结那次 tools 为空
    assert len(llm.calls[0][1]) == 1
    assert len(llm.calls[1][1]) == 1
    assert llm.calls[2][1] == []

    # 账本：system/user，然后 (assistant, tool) x2，最后一条总结 assistant
    assert [m.role for m in history.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    last_assistant = [m for m in history.messages() if m.role == "assistant"][-1]
    assert last_assistant.text == "这是最终总结"
    assert last_assistant.tool_calls == []


async def test_cancel_during_streaming_discards_half_message() -> None:
    history = InMemoryHistory()
    loop = AgentLoop(HangingStreamingLLM(), ToolRegistry(), history=history)
    events: list[AgentEvent] = []
    async for event in loop.run("问"):
        events.append(event)
        if event.type == "text_delta":
            loop.cancel()  # 模型只吐了半句就取消

    final = events[-1]
    assert final.type == "result"
    assert final.finish_reason == FinishReason.CANCELLED
    # 半句已经推给 UI 展示
    assert any(e.type == "text_delta" for e in events)
    # 但半截 assistant 消息绝不入账
    assert not any(e.type == "assistant_message" for e in events)
    assert [m.role for m in history.messages()] == ["system", "user"]


async def test_cancel_during_tool_closes_call_and_state_stays_consistent() -> None:
    hanging = HangingTool()
    registry = ToolRegistry()
    registry.register(hanging)
    llm = ScriptedFakeStreamingLLM([FakeTurn(tool_calls=[("hanging", {})])])
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)

    events: list[AgentEvent] = []
    async for event in loop.run("跑"):
        events.append(event)
        if event.type == "tool_call":
            loop.cancel()  # 工具执行中取消

    final = events[-1]
    assert final.type == "result"
    assert final.finish_reason == FinishReason.CANCELLED

    # 在飞工具真的被掐断，且该 tool_call 闭合为一条取消结果（不悬空）
    assert hanging.cancelled is True
    tool_results = [e for e in events if e.type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].call_id == "call_1_0"
    assert tool_results[0].ok is False

    # 账本停在完整边界：assistant（含调用）+ 配对的 tool 结果
    assert [m.role for m in history.messages()] == ["system", "user", "assistant", "tool"]
    assert history.messages()[-1].is_error is True
    # 取消后不再请求模型
    assert len(llm.calls) == 1

    # 账本完整、可据此开新 run 继续（同一个 history、换一个 loop 也行；无悬空 tool_call）
    llm2 = ScriptedFakeStreamingLLM([FakeTurn(text="接着上次继续")])
    loop2 = AgentLoop(llm2, ToolRegistry(), history=history)
    resumed = await collect(loop2.run("继续"))
    assert resumed[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_slow_tool_is_cut_off_and_closed_as_timeout() -> None:
    """工具跑太久：补一条"超时"失败结果回灌给模型，而不是让整轮永远悬着。

    超时也是"数据"：模型拿到后可以换工具 / 拆小任务 / 直接回答，循环不替它做决定
    （Claude Code 的做法一致：超时回灌失败结果、不终止回合）。
    """
    slow = ScriptedTool("slow", result={"ok": True}, delay=30.0)
    registry = ToolRegistry()
    registry.register(slow)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("slow", {})]),
            FakeTurn(text="这个工具超时了，我换个办法回答你。"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history, tool_timeout=0.05)

    start = time.perf_counter()
    events = await collect(loop.run("跑个慢的"))
    elapsed = time.perf_counter() - start

    # 没被工具内部那 30 秒的等待拖住
    assert elapsed < 5.0
    [timeout_result] = [e for e in events if e.type == "tool_result"]
    assert timeout_result.name == "slow"
    assert timeout_result.ok is False
    assert "超时" in timeout_result.error
    # 每个 call 都闭合（不悬空），账本停在完整边界：
    # system/user + 第一轮 assistant(带调用) + 配对的超时结果 + 收尾那轮 assistant
    assert [m.role for m in history.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    # 模型读懂超时、自己收尾，循环照常走到 final_text
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
    assert events[-1].text == "这个工具超时了，我换个办法回答你。"


async def test_interactive_tool_is_not_cut_off_by_tool_timeout() -> None:
    """``interactive`` 身份的工具（"问用户一句"）不受 ``tool_timeout`` 管。

    为什么必须豁免：别的工具 2 分钟没干完当失败是对的（把决定权交回模型），
    但"等用户点一下"可能等五分钟也可能等你去倒杯水 —— 掐断它的后果是这一轮
    平白以"工具超时"收尾，用户明明还没看那个问题。
    """
    gate = asyncio.Event()
    registry = ToolRegistry()
    registry.register(InteractiveGateTool(gate))
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("ask_gate", {})]),
            FakeTurn(text="收到你的回答，接着办。"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history, tool_timeout=0.05)

    task = asyncio.ensure_future(collect(loop.run("问一句")))
    # 远超 0.05s 的工具超时：普通工具此时早被补一条"超时"失败了
    await asyncio.sleep(0.3)
    assert not task.done()

    gate.set()
    events = await task

    [result] = [e for e in events if e.type == "tool_result"]
    assert result.ok is True
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
    assert events[-1].text == "收到你的回答，接着办。"


async def test_interactive_tool_still_yields_to_cancel() -> None:
    """豁免了超时，取消就成了交互式工具唯一的出口 —— 这条必须仍然收得住。

    不守它的后果很直接：用户点"停止"，界面上的问题卡片一直转圈，这一轮再也
    结束不了（等于把"没有任何东西能把一轮永久挂住"这条保证开了一个洞）。
    """
    gate = asyncio.Event()
    registry = ToolRegistry()
    registry.register(InteractiveGateTool(gate))
    llm = ScriptedFakeStreamingLLM([FakeTurn(tool_calls=[("ask_gate", {})])])
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history, cancel_grace_period=0.05)

    events: list[AgentEvent] = []
    async for event in loop.run("问一句"):
        events.append(event)
        if event.type == "tool_call":
            loop.cancel()

    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.CANCELLED
    [result] = [e for e in events if e.type == "tool_result"]
    assert result.ok is False
    assert "取消" in (result.error or "")


async def test_cancel_is_hard_even_if_tool_ignores_cancellation() -> None:
    """取消是通知不是请求：工具拒不退出时，宽限期一到就不再等它，照样收尾。

    守住这条的意义：只要有一个工具吞掉取消信号（同步阻塞 / 自己 except 掉），
    主循环就会永远卡在"等工具"上 —— 用户点了取消却什么都没发生。
    """
    stubborn = StubbornTool()
    registry = ToolRegistry()
    registry.register(stubborn)
    llm = ScriptedFakeStreamingLLM([FakeTurn(tool_calls=[("stubborn", {})])])
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history, cancel_grace_period=0.05)

    events: list[AgentEvent] = []
    try:
        async for event in loop.run("跑"):
            events.append(event)
            if event.type == "tool_call":
                loop.cancel()
    finally:
        # 放遗孤任务一条生路，免得测试收尾时 asyncio 报 "Task was destroyed but pending"
        stubborn.release.set()
        await asyncio.sleep(0.05)

    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.CANCELLED
    # 取消信号确实发到了工具（它收到了，只是不配合）
    assert stubborn.cancel_signals >= 1
    # 但这个 call 照样被闭合，账本不停在半截
    tool_results = [e for e in events if e.type == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0].ok is False
    assert [m.role for m in history.messages()] == ["system", "user", "assistant", "tool"]
    assert history.messages()[-1].is_error is True


async def test_every_event_is_json_serializable() -> None:
    good = ScriptedTool("good", result={"k": "v"})
    bad = ScriptedTool("bad", raises=ToolError("坏了"))
    registry = ToolRegistry()
    registry.register(good)
    registry.register(bad)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="先做", tool_calls=[("good", {}), ("bad", {})]),
            FakeTurn(text="完成"),
        ],
        usage=Usage(input_tokens=10, output_tokens=5),
    )
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    events = await collect(loop.run("x"))
    payloads = []
    for event in events:
        # 不抛即通过：事件流要能直接落盘 / 跨进程传输
        serialized = json.dumps(event.model_dump(), ensure_ascii=False)
        payloads.append(json.loads(serialized))

    result = payloads[-1]
    assert result["type"] == "result"
    assert result["finish_reason"] == "final_text"  # Enum 序列化为字符串
    # 两轮各 (10,5)，终态报累加总数 (20,10)
    # 2026-10-12：用量增 cached_tokens（界面算"省了多少"用）——假模型没给，故为 null
    assert result["usage"] == {
        "input_tokens": 20,
        "output_tokens": 10,
        "cached_tokens": None,
    }

    tool_results = {(p["name"], p["ok"]) for p in payloads if p["type"] == "tool_result"}
    assert tool_results == {("good", True), ("bad", False)}


async def test_text_then_tool_keep_order_within_one_message() -> None:
    echo = ScriptedTool(
        "echo",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        result_fn=lambda a: {"echoed": a["text"]},
    )
    registry = ToolRegistry()
    registry.register(echo)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="我先说明", tool_calls=[("echo", {"text": "hi"})]),
            FakeTurn(text="好了"),
        ]
    )
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    events = await collect(loop.run("x"))
    kinds = [e.type for e in events]

    # 第一轮"先说后调"：该轮文本 delta 都在第一个工具宣告之前
    first_tool_call_idx = kinds.index("tool_call")
    deltas_before_tool = "".join(
        events[i].delta for i in range(first_tool_call_idx) if events[i].type == "text_delta"
    )
    assert deltas_before_tool == "我先说明"

    # 同一条 assistant 消息内，块顺序为 text -> tool_call
    first_assistant = next(e.message for e in events if e.type == "assistant_message")
    assert [b.type for b in first_assistant.blocks] == ["text", "tool_call"]
    assert first_assistant.text == "我先说明"
    assert first_assistant.tool_calls[0].name == "echo"


async def test_fatal_llm_error_ends_run_failed_without_traceback() -> None:
    llm = FailingStreamingLLM(LLMError("401 鉴权失败", retryable=False))
    history = InMemoryHistory()
    loop = AgentLoop(llm, ToolRegistry(), history=history)

    events: list[AgentEvent] = []
    # 关键：run 作为 async generator 不应把异常抛给消费者
    async for event in loop.run("hi"):
        events.append(event)

    kinds = [e.type for e in events]
    assert kinds[0] == "run_started"
    error_events = [e for e in events if e.type == "error"]
    assert len(error_events) == 1
    assert "鉴权失败" in error_events[0].message

    final = events[-1]
    assert final.type == "result"
    assert final.finish_reason == FinishReason.FAILED

    # 错误发生在第一条模型轮，半截 assistant 没入账，账本停在 system/user
    assert [m.role for m in history.messages()] == ["system", "user"]


# ---------- 接缝新增：终态 / 取消必须落进 history ----------


async def test_run_persists_turn_end_with_reason_iterations_usage() -> None:
    """一次正常 run 收尾：把终态原因 / 轮数 / 用量 / 耗时记进 history（供落盘与回看）。"""
    echo = ScriptedTool(
        "echo",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        result_fn=lambda a: {"echoed": a["text"]},
    )
    registry = ToolRegistry()
    registry.register(echo)
    llm = ScriptedFakeStreamingLLM(
        [FakeTurn(tool_calls=[("echo", {"text": "hi"})]), FakeTurn(text="好了")],
        usage=Usage(input_tokens=11, output_tokens=7),
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)

    await collect(loop.run("x"))

    assert len(history.turn_ends) == 1
    ending = history.turn_ends[0]
    assert ending["reason"] == FinishReason.FINAL_TEXT
    assert ending["iterations"] == 2
    assert ending["duration_ms"] >= 0
    # 两轮各 (11,7) 必须累加，不能只留最后一轮
    assert ending["usage"] == Usage(input_tokens=22, output_tokens=14)
    assert history.cancels == []  # 没取消


async def test_total_usage_sums_across_tool_rounds() -> None:
    """多轮（工具轮 + 最终轮）token 用量逐轮累加，进终态事件与 turn_end 落盘。"""
    good = ScriptedTool("good", result={"v": 1})
    registry = ToolRegistry()
    registry.register(good)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("good", {})], usage=Usage(input_tokens=20, output_tokens=5)),
            FakeTurn(text="完成", usage=Usage(input_tokens=30, output_tokens=10)),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)

    events = await collect(loop.run("x"))
    result = events[-1]

    assert result.type == "result"
    assert result.usage == Usage(input_tokens=50, output_tokens=15)
    assert history.turn_ends[0]["usage"] == Usage(input_tokens=50, output_tokens=15)


async def test_total_usage_sums_cached_tokens_across_rounds() -> None:
    """命中缓存的 token 数同样逐轮累加：状态条上的"这个会话省了多少"用的是它。"""
    good = ScriptedTool("good", result={"v": 1})
    registry = ToolRegistry()
    registry.register(good)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(
                tool_calls=[("good", {})],
                usage=Usage(input_tokens=20, output_tokens=5, cached_tokens=12),
            ),
            FakeTurn(
                text="完成",
                usage=Usage(input_tokens=30, output_tokens=10, cached_tokens=28),
            ),
        ]
    )

    events = await collect(AgentLoop(llm, registry, history=InMemoryHistory()).run("x"))

    # 逐字段断言：pydantic 默认忽略未知字段，整模型相等会让"字段根本不存在"也照样通过
    assert events[-1].usage.input_tokens == 50
    assert events[-1].usage.output_tokens == 15
    assert events[-1].usage.cached_tokens == 40


async def test_total_usage_cached_stays_unknown_when_never_reported() -> None:
    """一轮都没回过缓存信息时保持 None：状态条显示"未知"，绝不谎报 0%。"""
    llm = ScriptedFakeStreamingLLM(
        [FakeTurn(text="好", usage=Usage(input_tokens=10, output_tokens=2))]
    )

    events = await collect(AgentLoop(llm, ToolRegistry(), history=InMemoryHistory()).run("x"))

    assert events[-1].usage.cached_tokens is None


async def test_result_context_size_uses_last_turn_input_not_sum() -> None:
    """上下文用量环的分子 = 最后一轮实际发送的 input_tokens（≈当前上下文大小），
    区别于逐轮累加、用于计费的 usage 总量；分母是当前大脑的上下文窗口。"""
    good = ScriptedTool("good", result={"v": 1})
    registry = ToolRegistry()
    registry.register(good)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("good", {})], usage=Usage(input_tokens=20, output_tokens=5)),
            FakeTurn(text="完成", usage=Usage(input_tokens=30, output_tokens=10)),
        ]
    )
    events = await collect(AgentLoop(llm, registry, history=InMemoryHistory()).run("x"))
    result = events[-1]

    assert result.type == "result"
    assert result.usage.input_tokens == 50  # 计费口径：逐轮累加
    assert result.context_input_tokens == 30  # 环口径：只看最后一轮（当前上下文）
    # 假 llm 不带 model 属性：分母回退保守默认窗口，必须为正数（环永不除零）
    assert result.context_window == context_window_of(None)
    assert result.context_window > 0


async def test_result_context_window_follows_the_brain_model() -> None:
    """真实大脑（llm 带 model id）：环分母取能力表里该型号的上下文窗口，分子是本轮 input。"""
    llm = ScriptedFakeStreamingLLM(
        [FakeTurn(text="好", usage=Usage(input_tokens=40, output_tokens=5))]
    )
    llm.model = "qwen3.8-max"
    events = await collect(AgentLoop(llm, ToolRegistry(), history=InMemoryHistory()).run("x"))
    result = events[-1]

    assert result.type == "result"
    assert result.context_input_tokens == 40
    assert result.context_window == context_window_of("qwen3.8-max")


async def test_result_context_window_follows_context_mode_max() -> None:
    """Max 档：同一个大脑，环分母从标准档 30 万放大到档位上限 100 万。"""
    llm = ScriptedFakeStreamingLLM(
        [FakeTurn(text="好", usage=Usage(input_tokens=40, output_tokens=5))]
    )
    llm.model = "qwen3.8-max"
    events = await collect(
        AgentLoop(
            llm,
            ToolRegistry(),
            history=InMemoryHistory(),
            context_mode="max",
        ).run("x")
    )
    result = events[-1]

    assert result.type == "result"
    assert result.context_window == context_window_of("qwen3.8-max", "max")
    assert result.context_window == 1_000_000


async def test_cancel_is_recorded_before_turn_end() -> None:
    """流中途取消：记一条 cancel，再记一条 cancelled 的 turn_end；半截消息不入账。"""
    history = InMemoryHistory()
    loop = AgentLoop(HangingStreamingLLM(), ToolRegistry(), history=history)

    events: list[AgentEvent] = []
    async for event in loop.run("问"):
        events.append(event)
        if event.type == "text_delta":
            loop.cancel()

    assert events[-1].finish_reason == FinishReason.CANCELLED
    assert len(history.cancels) == 1
    assert history.cancels[0]["reason"] == "user"
    assert len(history.turn_ends) == 1
    assert history.turn_ends[0]["reason"] == FinishReason.CANCELLED


# ---------- 接缝新增：展示层（sink）故障不得反杀任务 ----------


class ExplodingSink:
    """on_event 每次都抛：模拟 UI 序列化失败 / SSE 队列炸掉。"""

    def __init__(self) -> None:
        self.calls = 0

    async def on_event(self, event) -> None:
        self.calls += 1
        raise RuntimeError("UI 崩了")


async def test_sink_failure_does_not_kill_run(caplog) -> None:
    sink = ExplodingSink()
    loop = make_loop([FakeTurn(text="你好")])

    with caplog.at_level(logging.WARNING, logger="glyph.runtime"):
        events = await collect(loop.run("嗨", sink=sink))

    # 任务照常跑完：yield 的事件流完整，不受 sink 故障影响
    kinds = [e.type for e in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "result"
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
    assert events[-1].text == "你好"
    # sink 确实每条都被调过、也都抛过
    assert sink.calls == len(events)
    # 不是静默吞掉：留下了警告日志
    assert any("sink" in rec.message.lower() for rec in caplog.records)


# ---------- 接缝新增：同一 loop 的并发 run 必须排队，不许写花账本 ----------


async def test_concurrent_runs_are_serialized_and_keep_history_order() -> None:
    gate = asyncio.Event()
    registry = ToolRegistry()
    registry.register(GateTool(gate))
    # A 两轮（gate 工具轮 + 总结轮），B 一轮纯文本；剧本按调用顺序消费
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("gate", {})]),
            FakeTurn(text="A完成"),
            FakeTurn(text="B完成"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)

    # 启动 A，推进到 gate 工具执行中（此时 A 已持锁、挂在闸门上）
    agen_a = loop.run("任务A")
    a_events: list[AgentEvent] = []
    while True:
        ev = await agen_a.__anext__()
        a_events.append(ev)
        if ev.type == "tool_call":
            break
    a_in_flight = asyncio.ensure_future(agen_a.__anext__())  # 进入工具批次，挂在 gate
    await asyncio.sleep(0.05)
    assert not a_in_flight.done()  # A 确实还在跑

    # A 占着循环时启动 B：B 必须排队，连第一条事件、第一条 user 都不能落
    agen_b = loop.run("任务B")
    b_first = asyncio.ensure_future(agen_b.__anext__())
    await asyncio.sleep(0.05)
    assert not b_first.done()  # 还在排队等锁
    assert [m.content for m in history.messages() if m.role == "user"] == ["任务A"]
    assert loop.busy is True  # A 在跑、B 排队期间对外是"忙"

    # 放行 A：A 先完整收尾、释放锁，B 才开始
    gate.set()
    a_events.append(await a_in_flight)
    async for ev in agen_a:
        a_events.append(ev)
    b_events: list[AgentEvent] = [await b_first]
    async for ev in agen_b:
        b_events.append(ev)

    assert a_events[-1].finish_reason == FinishReason.FINAL_TEXT
    assert a_events[-1].text == "A完成"
    assert b_events[-1].finish_reason == FinishReason.FINAL_TEXT
    assert b_events[-1].text == "B完成"
    assert loop.busy is False  # 两个 run 都收尾后空闲

    # 账本严格 A 整段在前、B 整段在后，无交错
    assert [m.role for m in history.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m.content for m in history.messages() if m.role == "user"] == ["任务A", "任务B"]


# ---------- 接缝新增：流中途断网，安全重发当前轮（账本本轮未入账，重发幂等） ----------


async def test_retryable_stream_error_retries_current_turn_and_resets() -> None:
    llm = FlakyStreamingLLM(1, [FakeTurn(text="完整答案")])
    history = InMemoryHistory()
    loop = AgentLoop(
        llm, ToolRegistry(), history=history, max_stream_retries=2, retry_backoff_base=0.0
    )

    events = await collect(loop.run("问"))

    # 一次失败 + 一次成功：模型被调两次（同一轮重发）
    assert len(llm.calls) == 2

    # 发一条 turn_retry（network，第 1 次重发），夹在作废半截与重发内容之间
    retry_events = [e for e in events if e.type == "turn_retry"]
    assert len(retry_events) == 1
    assert retry_events[0].reason == "network"
    assert retry_events[0].attempt == 1
    kinds = [e.type for e in events]
    assert kinds.index("turn_retry") < kinds.index("result")

    # 最终拿到完整答案
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "完整答案"

    # 账本只留一条完整 assistant，半截没有入账
    assert [m.role for m in history.messages()] == ["system", "user", "assistant"]
    assert history.messages()[-1].text == "完整答案"


async def test_retryable_stream_error_exhausting_budget_fails_cleanly() -> None:
    llm = FlakyStreamingLLM(5, [FakeTurn(text="不会到这")])  # 一直断
    history = InMemoryHistory()
    loop = AgentLoop(
        llm, ToolRegistry(), history=history, max_stream_retries=2, retry_backoff_base=0.0
    )

    events = await collect(loop.run("问"))

    # 首次 + 2 次重发 = 3 次调用，发 2 条 turn_retry
    assert len(llm.calls) == 3
    assert [e.attempt for e in events if e.type == "turn_retry"] == [1, 2]

    assert any(e.type == "error" for e in events)
    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.FAILED
    # 残消息始终没入账，账本停在 system/user，可据此开新 run
    assert [m.role for m in history.messages()] == ["system", "user"]


async def test_non_retryable_stream_error_fails_immediately_without_retry() -> None:
    llm = FlakyStreamingLLM(1, [FakeTurn(text="不会到这")], retryable=False)
    history = InMemoryHistory()
    loop = AgentLoop(
        llm, ToolRegistry(), history=history, max_stream_retries=2, retry_backoff_base=0.0
    )

    events = await collect(loop.run("问"))

    assert len(llm.calls) == 1  # 4xx / 鉴权类：不重发
    assert not [e for e in events if e.type == "turn_retry"]
    assert events[-1].finish_reason == FinishReason.FAILED


# ---------- 接缝新增：输出截断（length）静默升档，复用本轮重发 ----------


async def test_length_truncation_silently_upgrades_max_tokens() -> None:
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="半截答案", finish="length"),
            FakeTurn(text="完整答案"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        max_output_tokens=100,
        max_output_tokens_upgrade=200,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("写长点"))

    # 首次用基础额度被截断；静默升档后重发当前轮，用升级额度
    assert [call[2] for call in llm.calls] == [100, 200]

    retry = [e for e in events if e.type == "turn_retry"]
    assert len(retry) == 1
    assert retry[0].reason == "max_tokens"
    assert retry[0].attempt == 1

    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "完整答案"

    # 半截没入账，账本只留一条完整 assistant
    assert [m.role for m in history.messages()] == ["system", "user", "assistant"]
    assert history.messages()[-1].text == "完整答案"


async def test_still_truncated_after_upgrade_continues_in_next_turn_and_joins_text() -> None:
    # 基础额度截断 → 升档重发仍截断 → 续给下一轮（摘工具接着说），最终文本拼接
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="第一段（升档重发后被丢弃）", finish="length"),
            FakeTurn(text="第二段还是很长", finish="length"),
            FakeTurn(text="，这是续写的结尾"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        max_output_tokens=10,
        max_output_tokens_upgrade=20,
        max_continuations=1,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("写一篇很长的"))

    # 三次模型调用：基础额度、升档重发（高档）、续写轮（直接高档、摘工具）
    assert [call[2] for call in llm.calls] == [10, 20, 20]

    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "第二段还是很长，这是续写的结尾"

    # Bug A：内部续写指令 transient——续写当轮发给模型，但不落盘、不进历史回放
    # 续写轮（第3次调用）发给模型：原始 user → 半截 assistant → 临时续写 user
    third = llm.calls[2][0]
    assert [m.role for m in third] == ["system", "user", "assistant", "user"]
    assert third[-1].content.startswith("（上一条回复因长度限制")

    # 最终账本：原始 user → 半截 assistant → 续写 assistant（没有"请继续"假 user）
    roles = [m.role for m in history.messages()]
    assert roles == ["system", "user", "assistant", "assistant"]
    persisted = [str(getattr(m, "content", getattr(m, "text", ""))) for m in history.messages()]
    assert not any("上一条回复因长度限制" in c for c in persisted)


async def test_continuation_cap_stops_loop_and_returns_what_was_generated() -> None:
    # 续写一次后仍被截断：不再无限续写，用已生成的全部内容正常收尾
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="第一段", finish="length"),
            FakeTurn(text="第二段", finish="length"),
            FakeTurn(text="第三段", finish="length"),  # 续写轮仍截断
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        max_output_tokens=10,
        max_output_tokens_upgrade=20,
        max_continuations=1,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("写"))

    assert len(llm.calls) == 3  # 没有第 4 次无限续写
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "第二段第三段"


# ---------- 接缝新增：流式空闲超时（连上了但服务端装死：不是断网，是一个字都不再吐） ----------


async def test_idle_stream_times_out_and_retries_then_succeeds() -> None:
    llm = StalledStreamingLLM(1, [FakeTurn(text="完整答案")])
    history = InMemoryHistory()
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        stream_idle_timeout=0.05,
        max_stream_retries=2,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("问"))

    # 第一次静默被空闲超时掐断、重发当前轮，第二次正常：模型共被调两次
    assert len(llm.calls) == 2
    retry = [e for e in events if e.type == "turn_retry"]
    assert len(retry) == 1
    assert retry[0].reason == "idle_timeout"
    assert retry[0].attempt == 1

    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "完整答案"
    # 静默轮吐到一半的 delta 没入账，账本只有重发后的完整 assistant
    assert [m.role for m in history.messages()] == ["system", "user", "assistant"]
    assert history.messages()[-1].text == "完整答案"


async def test_idle_stream_retries_exhausted_then_failed() -> None:
    llm = StalledStreamingLLM(5, [FakeTurn(text="不会到这")])  # 一直装死
    history = InMemoryHistory()
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        stream_idle_timeout=0.05,
        max_stream_retries=2,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("问"))

    # 首次 + 2 次重发 = 3 次调用、2 条 idle_timeout 重发提示
    assert len(llm.calls) == 3
    retries = [e for e in events if e.type == "turn_retry"]
    assert [e.attempt for e in retries] == [1, 2]
    assert all(e.reason == "idle_timeout" for e in retries)
    assert any(e.type == "error" for e in events)
    assert events[-1].finish_reason == FinishReason.FAILED
    # 残消息始终没入账，账本停在 system/user，可据此开新 run
    assert [m.role for m in history.messages()] == ["system", "user"]


async def test_slow_but_active_stream_not_killed_by_idle_timeout() -> None:
    # 每个字间隔 0.03s（远小于空闲阈值），12 个字总耗时约 0.36s（已远超阈值）
    llm = SlowStreamingLLM("一二三四五六七八九十一二", interval=0.03)
    history = InMemoryHistory()
    loop = AgentLoop(llm, ToolRegistry(), history=history, stream_idle_timeout=0.15)

    events = await collect(loop.run("问"))

    # 慢但一直在说就不是"卡死"：不许重发、不许掐断（证明统计的是事件间静默而非总时长）
    assert not [e for e in events if e.type == "turn_retry"]
    assert len(llm.calls) == 1
    result = events[-1]
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "一二三四五六七八九十一二"


async def test_cancel_during_idle_window_wins_over_timeout() -> None:
    # 空闲阈值很短，但用户在静默期立刻取消：必须立即 CANCELLED，不能被当成 idle 去重试
    llm = StalledStreamingLLM(5, [FakeTurn(text="不会到这")])
    history = InMemoryHistory()
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        stream_idle_timeout=1.0,  # 即使阈值只有 1s
        max_stream_retries=2,
        retry_backoff_base=0.0,
    )

    events: list[AgentEvent] = []
    start = time.perf_counter()
    async for event in loop.run("问"):
        events.append(event)
        if event.type == "text_delta":
            loop.cancel()  # 模型吐了半句就静默，立刻取消（远早于 1s 超时）
    elapsed = time.perf_counter() - start

    final = events[-1]
    assert final.finish_reason == FinishReason.CANCELLED
    assert not [e for e in events if e.type == "turn_retry"]  # 没走空闲超时重试
    assert len(llm.calls) == 1  # 取消后不再请求模型
    assert elapsed < 1.0  # 不必干等整个超时窗口


# ---------- 落盘失败：不许把整个循环炸掉（P0） ----------


class ExplodingHistory(InMemoryHistory):
    """模拟磁盘满 / 权限 / 会话已关闭：写账本必抛。"""

    async def record_user_message(self, text: str) -> None:
        raise SessionWriteError("模拟：磁盘写不进去了")


async def test_history_write_failure_ends_run_as_failed() -> None:
    """落盘写不进去时：发 error + 以 failed 收尾，绝不裸崩让前端永远转圈。"""
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="本来要说的回答")]),
        ToolRegistry(),
        history=ExplodingHistory(),
    )

    events = await collect(loop.run("问"))

    errors = [e for e in events if e.type == "error"]
    assert len(errors) == 1
    # 给出去的是人话，不是堆栈
    assert errors[0].message
    result = events[-1]
    assert result.type == "result"
    assert result.finish_reason == FinishReason.FAILED


# ---------- D16 接缝：loop 不许调用 HistoryPort 之外的方法 ----------


def test_loop_only_uses_methods_declared_on_history_port() -> None:
    """主循环对历史对象的每一次调用，都必须是端口声明过的方法。

    D16 的意义：将来换一个"只实现端口"的 history（比如带压缩的实现、远端实现），
    loop 一行不用改就能跑。只要 loop 偷偷用了端口外的东西，这个承诺就是空的。
    """
    source = pathlib.Path(loop_module.__file__ or "").read_text(encoding="utf-8")
    used = set(re.findall(r"self\.history\.(\w+)\s*\(", source))

    assert used, "没扫到 self.history.* 调用 —— 测试本身失效了，别让它假装通过"
    # 用 vars() 读端口自己的成员表：__protocol_attrs__ 是 3.12 才有的私有缓存属性，
    # 而本项目声明支持 3.11，直接取会在低版本上炸。
    declared = {
        name
        for name, member in vars(HistoryPort).items()
        if not name.startswith("_") and callable(member)
    }
    assert declared, "端口成员表是空的 —— 测试本身失效了"
    assert used <= declared, (
        f"loop 用了端口没声明的方法：{sorted(used - declared)}。"
        f"要么把方法并进 HistoryPort，要么改回用端口内的方法承载"
    )


class _PortOnlyHistory:
    """只实现 HistoryPort 声明的方法，多一个都不给。

    模拟"第三方只照着端口写了一个实现"——这是 D16 承诺要支持的用法。
    """

    def __init__(self, inner: InMemoryHistory) -> None:
        self._inner = inner

    def messages(self) -> list[Message]:
        return self._inner.messages()

    def set_transient_user_message(self, text: str) -> None:
        self._inner.set_transient_user_message(text)

    async def record_user_message(self, text: str) -> None:
        await self._inner.record_user_message(text)

    async def record_assistant_message(self, message: AssistantMessage) -> None:
        await self._inner.record_assistant_message(message)

    async def record_reasoning(self, text: str) -> None:
        await self._inner.record_reasoning(text)

    async def record_tool_result(self, call_id: str, result: ToolResult) -> None:
        await self._inner.record_tool_result(call_id, result)

    async def record_turn_end(
        self,
        *,
        reason: FinishReason,
        duration_ms: float,
        usage: Usage | None = None,
        context_input_tokens: int | None = None,
        iterations: int,
    ) -> None:
        await self._inner.record_turn_end(
            reason=reason,
            duration_ms=duration_ms,
            usage=usage,
            context_input_tokens=context_input_tokens,
            iterations=iterations,
        )

    async def record_cancel(self, *, reason: str = "user", iteration: int | None = None) -> None:
        await self._inner.record_cancel(reason=reason, iteration=iteration)

    def plan_compaction(
        self,
        *,
        effective_window: int,
        last_input_tokens: int | None,
        last_output_tokens: int | None,
        estimated_tokens: int | None = None,
        new_user_estimated_tokens: int = 0,
    ) -> CompactionPlan:
        return self._inner.plan_compaction(
            effective_window=effective_window,
            last_input_tokens=last_input_tokens,
            last_output_tokens=last_output_tokens,
            estimated_tokens=estimated_tokens,
            new_user_estimated_tokens=new_user_estimated_tokens,
        )

    async def commit_compaction(
        self,
        plan: CompactionPlan,
        summary_text: str,
        *,
        model: str | None = None,
    ) -> CompactionReport:
        return await self._inner.commit_compaction(plan, summary_text, model=model)

    def emergency_view(self, *, effective_window: int, strength: int = 0):
        return self._inner.emergency_view(effective_window=effective_window, strength=strength)


async def test_port_only_history_can_drive_continuation() -> None:
    """续写分支上，只实现端口的 history 也必须能跑完（不能半路 AttributeError）。"""
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="第一段", finish="length"),
            FakeTurn(text="第二段还是很长", finish="length"),
            FakeTurn(text="，续写的结尾"),
        ]
    )
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=_PortOnlyHistory(InMemoryHistory()),
        max_output_tokens=10,
        max_output_tokens_upgrade=20,
        max_continuations=1,
        retry_backoff_base=0.0,
    )

    events = await collect(loop.run("写一篇很长的"))

    result = events[-1]
    assert result.type == "result"
    assert result.finish_reason == FinishReason.FINAL_TEXT
    assert result.text == "第二段还是很长，续写的结尾"

    # 光"没崩"不够：续写指令得真到了模型嘴里（否则空实现也能假装通过）
    continuation_round = llm.calls[-1][0]
    assert continuation_round[-1].role == "user"
    assert continuation_round[-1].content == CONTINUATION_PROMPT

    # 同时它必须只是临时的：账本里不留一条用户没说过的"请继续"
    assert all(
        getattr(m, "content", None) != CONTINUATION_PROMPT
        for m in loop.history.messages()  # type: ignore[attr-defined]
    )


# ---------- 外层强行取消（客户端掉线）也必须掐断在飞工具（2026-10-01 修） ----------


class _BlockingBehavior:
    """开始后一直挂起、直到被取消的行为；记录"是否真的收到取消信号"。

    与 fakes 里的 StubbornBehavior 同一思路：行为只写一份，只读 / 写入两种身份
    各建一个子类去用（身份必须是类属性，没法运行时传参）。
    """

    name = "blocking"
    description = "开始后挂起，直到被取消"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self, args: dict) -> dict:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {}  # pragma: no cover


class _BlockingReadTool(_BlockingBehavior, ReadOnlyTool):
    name = "blocking"


class _BlockingWriteTool(_BlockingBehavior, WriteTool):
    name = "blocking_write"


async def _consume_until_tool_starts(loop: AgentLoop, tool: _BlockingBehavior) -> asyncio.Task:
    """起一个消费任务，等工具**真的跑起来**再把任务交回去。

    不等就取消，取消可能打在"还没进工具"的窗口上，测不到想测的那条路径。
    """

    async def consume() -> None:
        async for _ in loop.run("跑"):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.wait_for(tool.started.wait(), timeout=5)
    return task


async def test_external_cancel_stops_inflight_readonly_tool() -> None:
    """**外层直接 cancel** 也要掐断在飞的只读工具（并行路径）。

    旧实现只覆盖了"协作式取消"—— `loop.cancel()` 置事件、由 `_cancel_waiter()` 观察到，
    然后主动 cancel 工具任务。外层直接 cancel 时（客户端掉线后 web 层掐掉这次 run），
    `await asyncio.wait(...)` 会立刻抛 CancelledError 穿透出去，收尾逻辑一行都不执行，
    在飞的工具一路跑完。`grade_text` 这类最长 120 秒且按次计费，白跑一次就是白花钱。
    """
    tool = _BlockingReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = ScriptedFakeStreamingLLM([FakeTurn(tool_calls=[("blocking", {})])])
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    task = await _consume_until_tool_starts(loop, tool)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - 取消没传出去本身就是错
        raise AssertionError("外层取消没有被传播出去")

    assert tool.cancelled is True, "外层取消必须把在飞的只读工具掐断"


async def test_external_cancel_stops_inflight_writing_tool() -> None:
    """同一条保证在**串行路径**（含写入工具的批次）上也要成立。"""
    tool = _BlockingWriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = ScriptedFakeStreamingLLM([FakeTurn(tool_calls=[("blocking_write", {})])])
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    task = await _consume_until_tool_starts(loop, tool)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover
        raise AssertionError("外层取消没有被传播出去")

    assert tool.cancelled is True, "外层取消必须把在飞的写入工具掐断"
