"""测试替身：流式假模型与假工具。确定性、不联网（铁律 R6）。

ScriptedFakeStreamingLLM 站在"适配层输出"的位置：它直接产出适配层拼好的语义事件
（文本碎片、参数完整的工具调用、轮结束），因此 loop 的测试不关心厂商 SSE 细节。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from glyph.context import ContextFragment, render_context_message
from glyph.llm.base import LLMError, TextDelta, ToolCallReady, TurnEnd
from glyph.session.compaction_l3 import CompactionPlan, CompactionReport, EmergencyView
from glyph.tools.base import ReadOnlyTool, WriteTool
from glyph.types import (
    AssistantMessage,
    FinishReason,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    Usage,
    UserMessage,
)


@dataclass
class FakeTurn:
    """一轮模型输出的剧本。"""

    text: str | None = None
    # (工具名, 参数 dict) 列表；非空则这轮 stop_reason=tool_calls
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    chunk_size: int = 2
    # 本轮单独的用量；不给则回退到 ScriptedFakeStreamingLLM 的默认 usage
    usage: Usage | None = None
    # 显式指定结束原因（如 "length" 模拟输出截断）；不给则按 tool_calls/stop 推断
    finish: str | None = None
    # 直接指定本轮要吐出的内部事件序列（如平台 web_search 三态）；给了就原样产出、
    # 跳过 text/tool_calls 的自动拼装，调用方自己负责放 TurnEnd
    raw_events: list | None = None

    @property
    def stop_reason(self) -> str:
        if self.finish is not None:
            return self.finish
        return "tool_calls" if self.tool_calls else "stop"


class ScriptedFakeStreamingLLM:
    """按剧本逐轮产出流式事件；每次 stream_chat 顺序消费一个 FakeTurn。

    同时记录每次调用收到的 messages / tools，供测试断言"结果是否回灌"。
    """

    def __init__(self, turns: list[FakeTurn], *, usage: Usage | None = None) -> None:
        self._turns = list(turns)
        self.calls: list[tuple[list, list]] = []
        self._usage = usage

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        if not self._turns:
            raise AssertionError("ScriptedFakeStreamingLLM 剧本已耗尽：loop 多调了一次模型")
        turn = self._turns.pop(0)
        call_index = len(self.calls) + 1
        self.calls.append((list(messages), list(tools), max_tokens))

        if turn.raw_events is not None:
            # 调用方自备完整事件序列（含平台 web_search 等非文本事件），原样流式产出
            for chunk in turn.raw_events:
                yield chunk
                await asyncio.sleep(0)
            return

        if turn.text:
            for i in range(0, len(turn.text), max(1, turn.chunk_size)):
                yield TextDelta(delta=turn.text[i : i + turn.chunk_size])
                await asyncio.sleep(0)  # 让出事件循环，模拟真实流式的交错

        for idx, (name, args) in enumerate(turn.tool_calls):
            yield ToolCallReady(
                call=ToolCallBlock(id=f"call_{call_index}_{idx}", name=name, arguments=args)
            )
            await asyncio.sleep(0)

        turn_usage = turn.usage if turn.usage is not None else self._usage
        yield TurnEnd(stop_reason=turn.stop_reason, usage=turn_usage)


class ScriptedTool(ReadOnlyTool):
    """可配置的测试工具：记录每次入参，返回固定结果或由函数计算，可加延迟。

    继承 ``ReadOnlyTool`` 显式声明只读身份（R10）——不声明就会被 loop 按"写入"
    保守处理，从而走串行路径，让 test_loop.py 里那些验证"并行"的用例失去意义。
    """

    def __init__(
        self,
        name: str,
        *,
        result: dict | None = None,
        result_fn: Callable[[dict], Any] | None = None,
        parameters: dict | None = None,
        delay: float = 0.0,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.description = f"测试工具 {name}"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self._result = result if result is not None else {}
        self._result_fn = result_fn
        self.delay = delay
        self._raises = raises
        self.calls: list[dict] = []

    async def run(self, args: dict) -> Any:
        self.calls.append(args)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self._raises is not None:
            raise self._raises
        if self._result_fn is not None:
            return self._result_fn(args)
        return self._result


class HangingTool(ReadOnlyTool):
    """一直挂起、直到被取消的工具；记录自己是否真的收到 CancelledError。"""

    name = "hanging"
    description = "挂起直到取消"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.cancelled = False

    async def run(self, args: dict) -> dict:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {}  # pragma: no cover


class StubbornBehavior:
    """吞掉取消信号、拒不退出的**行为**（直到测试 ``release`` 才收摊）。

    把它从身份里拆出来，是为了让"只读版 / 写入版"共用同一份实现：两种身份的差异
    只是 ``readonly`` 这个类属性，而身份必须是类属性、不能运行时传参，所以只能各建
    一个子类。行为只此一份，杜绝将来"改了只读那份、忘了写入那份"。
    """

    name = "stubborn"
    description = "无视取消信号，直到被放行"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancel_signals = 0

    async def run(self, args: dict) -> dict:
        while not self.release.is_set():
            try:
                await asyncio.wait_for(self.release.wait(), timeout=0.01)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                self.cancel_signals += 1  # 收到了，但就是不退
                continue
        return {"ok": True}


class StubbornTool(StubbornBehavior, ReadOnlyTool):
    """只读身份的顽固工具（验证并行路径的硬取消；行为与历史上完全一致）。"""


class StubbornWriteTool(StubbornBehavior, WriteTool):
    """写入身份的顽固工具：串行路径里"工具吞掉取消信号"这一幕专用。"""

    name = "stubborn_write"


@dataclass(frozen=True)
class TimelineSpan:
    """一个工具这次执行的实际区间（共享 perf_counter 时钟，单位秒）。"""

    name: str
    start: float
    end: float


class TimelineRecorder:
    """记录每个工具进出时的墙钟时刻，供测试判断"有没有真的并行"。

    为什么不直接断言总耗时：总耗时是机器快慢的函数，在不同负载的 CI 上会抖。
    而"两个执行区间是否相交"是并发的**定义**本身 —— 全只读批次必须相交，
    含写入批次必须两两不相交，跟机器快慢无关。
    """

    def __init__(self) -> None:
        self.spans: list[TimelineSpan] = []

    def add(self, name: str, start: float, end: float) -> None:
        self.spans.append(TimelineSpan(name=name, start=start, end=end))

    @property
    def names(self) -> list[str]:
        """按开始时刻排序的工具名序列。串行路径下它必须等于模型声明的顺序。"""
        return [span.name for span in sorted(self.spans, key=lambda s: s.start)]

    def overlapping_pairs(self) -> list[tuple[str, str]]:
        """返回所有存在时间重叠的工具名对。空列表 == 两两严格不重叠。"""
        ordered = sorted(self.spans, key=lambda s: s.start)
        pairs: list[tuple[str, str]] = []
        for i, earlier in enumerate(ordered):
            for later in ordered[i + 1 :]:
                # 已按开始时刻排序故 later.start >= earlier.start；相交 <=>
                # later 开跑时 earlier 还没结束。恰好首尾相接（==）不算并行。
                if later.start < earlier.end:
                    pairs.append((earlier.name, later.name))
        return pairs

    def any_overlap(self) -> bool:
        """是否存在任意两个工具同时在跑。"""
        return bool(self.overlapping_pairs())

    def span_seconds(self) -> float:
        """从最早的开始到最后结束的墙钟跨度（并发时约等于单个耗时，串行时约等于总和）。"""
        if not self.spans:
            return 0.0
        return max(s.end for s in self.spans) - min(s.start for s in self.spans)


class TimelineBehavior:
    """把自己的执行区间记进共享 recorder 的**行为**；只读 / 写入身份各挂一个子类。

    ``delay`` 用来制造可观测的时间宽度；``raises`` 用来制造"执行过但失败了"的场景 ——
    失败的工具也算执行过，所以区间在 ``finally`` 里记，正常 / 抛错都留下痕迹。
    """

    description = "记录自己在什么时间段真的跑着"
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        name: str,
        *,
        recorder: TimelineRecorder,
        delay: float = 0.05,
        result: dict | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.recorder = recorder
        self.delay = delay
        self._result = result if result is not None else {}
        self._raises = raises
        self.calls: list[dict] = []

    async def run(self, args: dict) -> dict:
        self.calls.append(args)
        started = time.perf_counter()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self._raises is not None:
                raise self._raises
            return self._result
        finally:
            self.recorder.add(self.name, started, time.perf_counter())


class TimelineReadTool(TimelineBehavior, ReadOnlyTool):
    """只读身份 + 时间线：证明"批次全是只读 → 仍然并发"。"""


class TimelineWriteTool(TimelineBehavior, WriteTool):
    """写入身份 + 时间线：证明"批次里有写入 → 整批串行、严格不重叠"。"""


class GateTool(ReadOnlyTool):
    """卡在一个 asyncio.Event 上直到测试 set；确定性地制造"任务正占着循环"。"""

    name = "gate"
    description = "挂起直到闸门打开"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def run(self, args: dict) -> dict:
        await self._gate.wait()
        return {"ok": True}


class InteractiveGateTool(GateTool):
    """跟 GateTool 一样卡在闸门上，但带 ``interactive`` 身份。

    差别只有那一个类属性：``interactive`` 的工具（典型是"问用户一句"）不受
    ``tool_timeout`` 管 —— 等一个人点了选项可能要几分钟，不该在 2 分钟时被掐断。
    """

    name = "ask_gate"
    interactive: ClassVar[bool] = True


class HangingStreamingLLM:
    """吐出一个 delta 后就长时间静默的模型流（模拟网络等待 / 模型卡住）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[list, list]] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append((list(messages), list(tools)))
        yield TextDelta(delta="半句")
        await asyncio.sleep(3600)
        yield TurnEnd(stop_reason="stop")  # pragma: no cover - 取消前到不了


class FailingStreamingLLM:
    """一开始取流就抛给定异常（模拟建连失败 / 4xx / 重试耗尽）。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[tuple[list, list]] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append((list(messages), list(tools)))
        raise self.exc
        yield  # pragma: no cover - 使本函数成为 async generator


class FlakyStreamingLLM:
    """前 fail_times 次 stream_chat：吐半个 delta 后抛错（模拟流中途断网），之后按剧本正常。

    用于验证"本轮安全重发"：半截 delta 作废、账本不留残消息、重发后拿到完整答案。
    """

    def __init__(
        self,
        fail_times: int,
        good_turns: list[FakeTurn],
        *,
        retryable: bool = True,
        half_text: str = "半截",
    ) -> None:
        self._fail_times = fail_times
        self._failures = 0
        self._good = ScriptedFakeStreamingLLM(good_turns)
        self._retryable = retryable
        self._half_text = half_text
        self.calls: list[tuple[list, list]] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append((list(messages), list(tools)))
        if self._failures < self._fail_times:
            self._failures += 1
            yield TextDelta(delta=self._half_text)
            await asyncio.sleep(0)
            raise LLMError("连接被重置", retryable=self._retryable)
        async for event in self._good.stream_chat(messages, tools, max_tokens=max_tokens):
            yield event


class StalledStreamingLLM:
    """前 stall_times 次 stream_chat：吐一个开头 delta 后就长时间静默（连接还在、
    服务端却一个字都不再吐），之后按剧本正常。

    与 FlakyStreamingLLM 的区别：那是"流中途报错 / 断连"，这是"连上了但装死"，
    用来验证主循环的流式空闲超时（idle timeout）会把它当可恢复故障、重发当前轮。
    """

    def __init__(
        self, stall_times: int, good_turns: list[FakeTurn], *, half_text: str = "半句"
    ) -> None:
        self._stall_times = stall_times
        self._stalls = 0
        self._good = ScriptedFakeStreamingLLM(good_turns)
        self._half_text = half_text
        self.calls: list[tuple[list, list]] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append((list(messages), list(tools)))
        if self._stalls < self._stall_times:
            self._stalls += 1
            yield TextDelta(delta=self._half_text)
            await asyncio.sleep(0)
            await asyncio.sleep(3600)  # 静默到被主循环的空闲超时掐断
            return  # pragma: no cover - 取消前到不了
        async for event in self._good.stream_chat(messages, tools, max_tokens=max_tokens):
            yield event


class SlowStreamingLLM:
    """每个 token 之间都间隔 interval 秒、但始终在持续产出（慢而活跃）。

    用来证明空闲超时统计的是"两个事件之间的静默"，不是"整轮总时长"：只要每个间隔
    都短于阈值，即使总耗时远超阈值也不许掐断，否则会误杀正在认真思考的慢回复。
    """

    def __init__(self, text: str, *, interval: float = 0.02, usage: Usage | None = None) -> None:
        self._text = text
        self.interval = interval
        self._usage = usage
        self.calls: list[tuple[list, list]] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append((list(messages), list(tools)))
        for ch in self._text:
            yield TextDelta(delta=ch)
            await asyncio.sleep(self.interval)
        yield TurnEnd(stop_reason="stop", usage=self._usage)


class InMemoryHistory:
    """HistoryPort 的内存假实现：不写盘，行为对齐 SessionHistory 对 loop 可见的部分。

    - ``messages()`` 与真 SessionHistory 一样：system 在最前，工具结果按 call_id 回灌成
      ToolMessage；turn_end / cancel 只另存、不进 messages()。
    - "孤儿 tool_call 自动补错误结果"是真 SessionHistory 为硬中断重开准备的，由它自己的
      测试覆盖；loop 的协作式取消不会留下孤儿（流中途不记半截消息、工具中取消会全部闭合），
      所以假实现不重复那段逻辑，保持最小。
    """

    def __init__(
        self,
        system_prompt: str = "你是测试助手。",
        context: Sequence[ContextFragment] = (),
    ) -> None:
        self._messages: list = [SystemMessage(content=system_prompt)]
        self._context = list(context)
        self._transient: str | None = None
        self.turn_ends: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []
        # 思考落盘（展示用，对齐 SessionHistory：不进 messages()，只记着给人看）
        self.reasonings: list[str] = []

    def messages(self) -> list:
        # 对齐 SessionHistory（Bug B）：system 最前，上下文合成消息紧跟其后、在对话之前
        result: list = [self._messages[0]]
        context_message = render_context_message(self._context)
        if context_message is not None:
            result.append(context_message)
        result.extend(self._messages[1:])
        # 对齐 SessionHistory（Bug A）：续写指令只临时进这一次 messages()，不落盘
        if self._transient is not None:
            result.append(UserMessage(content=self._transient))
            self._transient = None
        return result

    def set_transient_user_message(self, text: str) -> None:
        """续写指令只临时进下一次 messages()，不写盘、不进历史回放。"""
        self._transient = text or None  # 空串等于清除，对齐 SessionHistory

    def _clear_transient(self) -> None:
        """对齐 SessionHistory：任何 record_* 落账也会清掉临时消息（两条清除路径取先到）。"""
        self._transient = None

    async def record_user_message(self, text: str) -> None:
        self._clear_transient()
        self._messages.append(UserMessage(content=text))

    async def record_assistant_message(self, message: AssistantMessage) -> None:
        self._clear_transient()
        self._messages.append(message)

    async def record_reasoning(self, text: str) -> None:
        """对齐 SessionHistory：思考只记着给人看，永远不进 messages()。"""
        self._clear_transient()
        self.reasonings.append(text)

    async def record_tool_result(self, call_id: str, result: ToolResult) -> None:
        self._clear_transient()
        if result.ok:
            content = json.dumps(result.output or {}, ensure_ascii=False)
        else:
            content = result.error or "工具执行失败"
        self._messages.append(
            ToolMessage(
                tool_call_id=call_id,
                name=result.tool_name,
                content=content,
                is_error=not result.ok,
            )
        )

    async def record_turn_end(
        self,
        *,
        reason: FinishReason,
        duration_ms: float,
        usage: Usage | None = None,
        context_input_tokens: int | None = None,
        iterations: int,
    ) -> None:
        self._clear_transient()
        self.turn_ends.append(
            {
                "reason": reason,
                "duration_ms": duration_ms,
                "usage": usage,
                "context_input_tokens": context_input_tokens,
                "iterations": iterations,
            }
        )

    async def record_cancel(self, *, reason: str = "user", iteration: int | None = None) -> None:
        self._clear_transient()
        self.cancels.append({"reason": reason, "iteration": iteration})

    # ---------- 上下文压缩端口（§12.2）：内存假实现默认永不压缩 ----------

    def plan_compaction(
        self,
        *,
        effective_window: int,
        last_input_tokens: int | None,
        last_output_tokens: int | None,
        estimated_tokens: int | None = None,
        new_user_estimated_tokens: int = 0,
    ) -> CompactionPlan:
        """默认 no-op：永远返回 normal，现有 loop 测试不触发任何压缩。

        需要测压缩接线的用例在外层包一个会返回 compact-l3 plan 的脚本历史
        （见 tests/runtime/test_compaction_loop.py）。
        """
        return CompactionPlan(
            state="normal",
            levels=(),
            predicted_tokens=last_input_tokens or estimated_tokens or 0,
            window_tokens=effective_window,
            reason="InMemoryHistory 默认不压缩",
        )

    async def commit_compaction(
        self,
        plan: CompactionPlan,
        summary_text: str,
        *,
        model: str | None = None,
    ) -> CompactionReport:
        return CompactionReport(
            success=False,
            level="",
            boundary_id="",
            at_seq=None,
            summary_record_seq=None,
            before_tokens=plan.predicted_tokens,
            after_tokens=None,
            degraded_reason="InMemoryHistory 不支持压缩落账",
        )

    def emergency_view(
        self, *, effective_window: int, strength: int = 0
    ) -> EmergencyView:
        """默认 no-op：内存假历史不做应急减重，明确不可行（测试一般不会走到 413 应急）。"""
        return EmergencyView(
            feasible=False,
            messages=[],
            estimated_tokens=0,
            kept_turns=0,
            protected_user_texts=(),
            strength=strength,
            reason="InMemoryHistory 不支持应急减重",
        )
