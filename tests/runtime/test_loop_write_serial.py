"""写入工具串行执行（铁律 R10）的行为守护。

为什么另起一个文件而不是塞进 ``test_loop.py``：这里六组断言关注的都是**同一批次里
工具的执行布局**（谁和谁并行、谁把整批拖成串行、卡死 / 被取消时剩下那些 call 怎么
闭合），跟"单个工具的结果怎么回灌给模型"不是同一件事，混在一起会让两边都难读。

判定手段统一用"时间线"（每个工具进出时的真实墙钟戳），而不是总耗时：总耗时是机器
快慢的函数，在负载不同的机器上会抖；而"两个执行区间是否相交"是并发的**定义**本身。

全部离线、确定性、不联网（铁律 R6）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from fakes import (
    FakeTurn,
    InMemoryHistory,
    ScriptedFakeStreamingLLM,
    StubbornWriteTool,
    TimelineBehavior,
    TimelineReadTool,
    TimelineRecorder,
    TimelineWriteTool,
)

from glyph.events import AgentEvent
from glyph.runtime.errors import ToolError
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.tool import Tool
from glyph.types import FinishReason

DELAY = 0.05  # 每个假工具的"工作量"：够宽到能可靠判定重叠与否


async def collect(run: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in run]


def make_loop(turns, registry, **kwargs) -> tuple[AgentLoop, InMemoryHistory]:
    """装配一个 loop 并返回它的假账本，便于断言"每个 call 都闭合"。"""
    history = InMemoryHistory()
    loop = AgentLoop(ScriptedFakeStreamingLLM(turns), registry, history=history, **kwargs)
    return loop, history


class UndeclaredTool(TimelineBehavior, Tool):
    """故意不声明身份的假工具 —— 模拟"作者忘了继承 ReadOnlyTool"的新工具。"""

    pass


async def test_all_readonly_batch_still_runs_concurrently() -> None:
    """全只读批次：三个工具的执行区间互相重叠 —— 证明现有并发行为没有退化。

    守的是"不要为了安全把整个 loop 都串行掉"：这条一旦红掉，说明我们为了防范
    写入工具，顺手把只读工具的吞吐也赔进去了。
    """
    recorder = TimelineRecorder()
    tools = [
        TimelineReadTool(f"read_{suffix}", recorder=recorder, delay=DELAY)
        for suffix in ("a", "b", "c")
    ]
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    loop, _ = make_loop(
        [
            FakeTurn(tool_calls=[("read_a", {}), ("read_b", {}), ("read_c", {})]),
            FakeTurn(text="三个都查完了"),
        ],
        registry,
    )

    events = await collect(loop.run("只读并行"))

    # 三个都真的跑过
    assert set(recorder.names) == {"read_a", "read_b", "read_c"}
    # 并发的本质证据：任意两个工具的区间相交（三对全相交）
    assert recorder.any_overlap() is True
    assert len(recorder.overlapping_pairs()) == 3
    # 总跨度约等于单个工具的耗时（≈0.05），明确小于串行所需的三者之和（≈0.15）
    assert recorder.span_seconds() < 0.12
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_batch_with_write_tool_is_strictly_sequential() -> None:
    """含写入工具的批次：两两严格不重叠，且顺序等于模型声明的顺序。

    顺带守 R10 那句"不得与同批次任何其他工具并行（**含只读工具**）"：本用例的中间
    那个是只读工具，它同样被拖成串行 —— 这是"整批串行"而不是"只有写工具之间串行"。
    """
    recorder = TimelineRecorder()
    first = TimelineWriteTool("write_1", recorder=recorder, delay=DELAY)
    middle = TimelineReadTool("read_mid", recorder=recorder, delay=DELAY)
    last = TimelineWriteTool("write_2", recorder=recorder, delay=DELAY)
    registry = ToolRegistry()
    for tool in (first, middle, last):
        registry.register(tool)
    loop, _ = make_loop(
        [
            FakeTurn(tool_calls=[("write_1", {}), ("read_mid", {}), ("write_2", {})]),
            FakeTurn(text="按顺序写完了"),
        ],
        registry,
    )

    events = await collect(loop.run("写两个、中间查一个"))

    # 串行：没有任何两个工具在同一时刻都跑着
    assert recorder.any_overlap() is False
    assert recorder.overlapping_pairs() == []
    # 顺序确定 == 模型声明的顺序（写入工具的顺序必须是可预测的）
    assert recorder.names == ["write_1", "read_mid", "write_2"]
    # 三段互不重叠 → 总跨度不小于三者之和
    assert recorder.span_seconds() >= DELAY * 3
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_stuck_write_tool_times_out_and_turn_continues() -> None:
    """串行批次里一个写入工具卡死：被收口成 ok=False 的失败结果，**回合继续**。

    守的是"串行路径不许比今天更危险"：串行之后，一个工具卡死本来会把整轮永久挂住
    （朴素 ``for + await`` 就是这样）。必须证明超时兜底在串行路径上也活着，而且
    排在它后面的工具仍然照常执行。
    """
    recorder = TimelineRecorder()
    stuck = TimelineWriteTool("write_stuck", recorder=recorder, delay=30.0)
    after = TimelineWriteTool("write_after", recorder=recorder, delay=0.01, result={"saved": True})
    registry = ToolRegistry()
    for tool in (stuck, after):
        registry.register(tool)
    loop, history = make_loop(
        [
            FakeTurn(tool_calls=[("write_stuck", {}), ("write_after", {})]),
            FakeTurn(text="第一个工具超时了，第二个写成功了。"),
        ],
        registry,
        tool_timeout=0.05,
    )

    start = time.perf_counter()
    events = await collect(loop.run("写两个，第一个卡住"))
    elapsed = time.perf_counter() - start

    # 没被工具内部那 30 秒的等待拖住
    assert elapsed < 5.0
    results = {e.name: e for e in events if e.type == "tool_result"}
    assert results["write_stuck"].ok is False
    assert "超时" in results["write_stuck"].error
    # 后面的写入工具真的执行了（不是"整批被卡死的那个拖累"）
    assert after.calls == [{}]
    assert results["write_after"].ok is True

    # 两个 call 都闭合，账本停在完整边界
    assert [m.role for m in history.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_serial_cancel_closes_remaining_calls_without_running_them() -> None:
    """串行批次中工具吞掉 CancelledError：后续工具拿到"未执行"，整体落 CANCELLED。

    这条最容易漏：写串行路径时如果只照搬"执行"而忘了"被叫停后剩下的不许再开跑"，
    用户点了取消，剩下的写入工具还是会照写一遍 —— 那正是 R10 要防的互相覆盖。
    同时每个 call 仍必须闭合，否则 loop 取 ``results[call.id]`` 会 KeyError 炸穿。
    """
    recorder = TimelineRecorder()
    stubborn = StubbornWriteTool()
    later = TimelineWriteTool("write_later", recorder=recorder, delay=0.01, result={"saved": True})
    registry = ToolRegistry()
    for tool in (stubborn, later):
        registry.register(tool)
    loop, history = make_loop(
        [FakeTurn(tool_calls=[("stubborn_write", {}), ("write_later", {})])],
        registry,
        cancel_grace_period=0.05,
    )

    events: list[AgentEvent] = []
    try:
        async for event in loop.run("写两个，第一个不配合取消"):
            events.append(event)
            if event.type == "tool_call":
                loop.cancel()
    finally:
        # 放遗孤任务一条生路，免得测试收尾时 asyncio 报 "Task was destroyed but pending"
        stubborn.release.set()
        await asyncio.sleep(0.05)

    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.CANCELLED
    # 取消信号确实发到了工具（它收到了，只是吞掉不退出）
    assert stubborn.cancel_signals >= 1
    # 关键：后一个写入工具根本没有开跑
    assert later.calls == []
    assert later.recorder.spans == []
    # 但它的 call 照样闭合：账本不停在半截，两条结果都有去向
    tool_results = [e for e in events if e.type == "tool_result"]
    assert {e.name for e in tool_results} == {"stubborn_write", "write_later"}
    assert all(e.ok is False for e in tool_results)
    assert [m.role for m in history.messages()] == ["system", "user", "assistant", "tool", "tool"]


async def test_write_tool_error_is_data_and_batch_still_sequential() -> None:
    """写入工具抛 ToolError / 返回错误 = 一条 ok=False 的结果（错误即数据），不炸循环。

    守的是 R5「错误即数据」在串行路径上同样成立：串行不等于把异常往上传播。
    """
    recorder = TimelineRecorder()
    boom = TimelineWriteTool(
        "write_boom", recorder=recorder, delay=0.01, raises=ToolError("记忆库写不进去")
    )
    saved = TimelineWriteTool("write_ok", recorder=recorder, delay=0.01, result={"saved": True})
    registry = ToolRegistry()
    for tool in (boom, saved):
        registry.register(tool)
    loop, history = make_loop(
        [
            FakeTurn(tool_calls=[("write_boom", {}), ("write_ok", {})]),
            FakeTurn(text="一条失败、一条成功"),
        ],
        registry,
    )

    events = await collect(loop.run("写两条"))

    results = {e.name: e for e in events if e.type == "tool_result"}
    assert results["write_boom"].ok is False
    assert "记忆库写不进去" in results["write_boom"].error
    assert results["write_ok"].ok is True
    assert results["write_ok"].output == {"saved": True}

    # 写失败的工具也算"执行过"，区间进记录 ⇒ 串行顺序仍可判定
    assert recorder.names == ["write_boom", "write_ok"]
    assert recorder.any_overlap() is False

    # 两条结果（含错误那条）都回灌给模型，让它自己决定下一步
    tool_msgs = [m for m in history.messages() if m.role == "tool"]
    assert len(tool_msgs) == 2
    assert sum(1 for m in tool_msgs if m.is_error) == 1
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_missing_identity_is_treated_as_write() -> None:
    """身份标记缺失 / 工具名查不到 → 一律按"写入"（串行）保守处理。

    守的方向刻意不对称：忘了声明的代价只是"慢一点"，标错的代价是"两个工具同时改
    同一个目标互相覆盖"。决定书上看的始终是"能不能并"，默认答案必须是不能。
    """
    registry = ToolRegistry()
    # 名字压根不存在（模型幻觉的工具名）：不算只读
    assert registry.is_readonly("不存在的工具") is False

    recorder = TimelineRecorder()
    undeclared = UndeclaredTool("undeclared", recorder=recorder, delay=DELAY)
    read_tool = TimelineReadTool("read_friend", recorder=recorder, delay=DELAY)
    registry.register(undeclared)
    registry.register(read_tool)
    # 没继承 ReadOnlyTool 的工具：不算只读（即使它其实什么都不写）
    assert registry.is_readonly("undeclared") is False
    assert registry.is_readonly("read_friend") is True

    loop, _ = make_loop(
        [
            FakeTurn(tool_calls=[("undeclared", {}), ("read_friend", {})]),
            FakeTurn(text="都跑完了"),
        ],
        registry,
    )
    events = await collect(loop.run("跑两个"))

    # 保守 ⇒ 整批串行：哪怕其中一个是货真价实的只读工具
    assert recorder.any_overlap() is False
    assert recorder.names == ["undeclared", "read_friend"]
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
