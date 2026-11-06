"""#66 主循环接线上下文压缩：CompactionEvent 事件、主动 L3 压缩时序与降级。

接缝铁律（设计稿 §12.2 / §13）：
- loop 只通过 HistoryPort 声明的 plan_compaction / commit_compaction 驱动压缩；
- 写手（便宜模型）由 loop 自己持有、history 不碰网络（B2）；
- L3 触发时 ``started`` 事件必须在写手请求**之前**发出（写手慢/卡住时用户能看到"正在浓缩"）；
- 写手失败 / 无写手 / 落账失败一律降级：发 failed/skipped 后本轮照常拿全量原文发送，绝不炸主循环。
"""

from __future__ import annotations

import asyncio

import pytest
from fakes import FakeTurn, InMemoryHistory, ScriptedFakeStreamingLLM, ScriptedTool
from pydantic import TypeAdapter

from glyph.events import AgentEvent, CompactionEvent, TurnRetryEvent
from glyph.llm.base import LLMError
from glyph.llm.compactor import ContextTooLongError, SummaryResult
from glyph.runtime.compaction_metrics import CompactionMetric
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.sink import CollectingSink
from glyph.session.compaction_l3 import CompactionPlan, CompactionReport, EmergencyView
from glyph.types import FinishReason, SystemMessage, Usage, UserMessage


async def collect(run) -> list:
    return [event async for event in run]


def l3_plan(**over) -> CompactionPlan:
    base = dict(
        state="compact",
        levels=("l3",),
        predicted_tokens=249_000,
        window_tokens=300_000,
        reason="over l3 line",
        at_seq=5,
        boundary_id="c-test123",
        tail_records=(),
        tail_turns=3,
        material_records=(object(), object(), object()),
        material_turn_count=2,
        material_truncated=False,
        seed_summary_seq=None,
        seed_summary_text=None,
    )
    base.update(over)
    return CompactionPlan(**base)


def summary(**over) -> SummaryResult:
    base = dict(
        text="浓缩摘要正文",
        model="qwen3.7-flash",
        input_tokens=1200,
        output_tokens=300,
        material_halved=False,
        finish_reason="stop",
    )
    base.update(over)
    return SummaryResult(**base)


class ScriptedCompactionHistory(InMemoryHistory):
    """按脚本逐次返回 plan（None 表示该次走默认 normal），并记录 plan/commit 调用。"""

    def __init__(self, plans, *, commit_ok: bool = True, emergency_factory=None, **kw) -> None:
        super().__init__(**kw)
        self._plans = list(plans)
        self.commit_ok = commit_ok
        self._emergency_factory = emergency_factory
        self.emergency_calls: list[int] = []
        self.plan_calls: list[dict] = []
        self.commit_calls: list[tuple] = []

    def plan_compaction(
        self,
        *,
        effective_window,
        last_input_tokens,
        last_output_tokens,
        estimated_tokens=None,
        new_user_estimated_tokens=0,
    ) -> CompactionPlan:
        self.plan_calls.append(
            {
                "window": effective_window,
                "in": last_input_tokens,
                "out": last_output_tokens,
                "est": estimated_tokens,
            }
        )
        if self._plans:
            planned = self._plans.pop(0)
            if planned is not None:
                return planned
        return super().plan_compaction(
            effective_window=effective_window,
            last_input_tokens=last_input_tokens,
            last_output_tokens=last_output_tokens,
            estimated_tokens=estimated_tokens,
            new_user_estimated_tokens=new_user_estimated_tokens,
        )

    async def commit_compaction(self, plan, summary_text, *, model=None) -> CompactionReport:
        self.commit_calls.append((plan, summary_text, model))
        if not self.commit_ok:
            return CompactionReport(
                success=False,
                level="l3",
                boundary_id=plan.boundary_id or "",
                at_seq=plan.at_seq,
                summary_record_seq=None,
                before_tokens=plan.predicted_tokens,
                after_tokens=None,
                degraded_reason="磁盘满，落账失败",
            )
        return CompactionReport(
            success=True,
            level="l3",
            boundary_id=plan.boundary_id or "c-x",
            at_seq=plan.at_seq,
            summary_record_seq=99,
            before_tokens=plan.predicted_tokens,
            after_tokens=120_000,
            material_truncated=plan.material_truncated,
        )

    def emergency_view(self, *, effective_window, strength: int = 0):
        self.emergency_calls.append(strength)
        if self._emergency_factory is not None:
            return self._emergency_factory(strength)
        return super().emergency_view(effective_window=effective_window, strength=strength)


class FakeCompactor:
    """记录调用顺序的假写手；可挂起 / 抛错 / 返回给定 SummaryResult。"""

    def __init__(self, result=None, *, raises=None, order=None) -> None:
        self._result = result
        self._exc = raises
        self.order = order
        self.calls: list[dict] = []

    async def summarize(
        self, *, material_records, seed_summary_text=None, material_truncated=False
    ):
        self.calls.append(
            {"n": len(material_records), "seed": seed_summary_text, "truncated": material_truncated}
        )
        if self.order is not None:
            self.order.append("summarize")
        await asyncio.sleep(0)
        if self._exc is not None:
            raise self._exc
        return self._result


class OrderSink(CollectingSink):
    """实时记录 compaction 事件上屏顺序，用于和写手调用时刻对比（C9）。"""

    def __init__(self, order: list) -> None:
        super().__init__()
        self.order = order

    async def on_event(self, event) -> None:
        await super().on_event(event)
        if getattr(event, "type", None) == "compaction":
            self.order.append(("event", event.phase))


def _compaction_phases(events: list) -> list[str]:
    return [e.phase for e in events if getattr(e, "type", None) == "compaction"]


def test_compaction_event_defaults_and_serialization() -> None:
    event = CompactionEvent(phase="started", level="l3", before_tokens=249_000)
    assert event.type == "compaction"
    assert event.phase == "started"
    assert event.level == "l3"
    assert event.before_tokens == 249_000
    assert event.after_tokens is None
    assert event.boundary_id is None
    assert event.material_truncated is False
    # 状态事件必须可 json 序列化（要落盘 / 走 SSE）
    dumped = event.model_dump_json()
    assert '"phase":"started"' in dumped.replace(" ", "")


def test_compaction_event_is_part_of_agent_event_union() -> None:
    adapter = TypeAdapter(AgentEvent)
    parsed = adapter.validate_python(
        {"type": "compaction", "phase": "done", "level": "l3", "after_tokens": 120_000}
    )
    assert isinstance(parsed, CompactionEvent)
    assert parsed.phase == "done"
    assert parsed.after_tokens == 120_000


@pytest.mark.parametrize("phase", ["started", "l2", "l3", "retrying", "done", "failed", "skipped"])
def test_compaction_event_accepts_all_documented_phases(phase: str) -> None:
    event = CompactionEvent(phase=phase, level="l3")
    assert event.phase == phase


# ---------- 主动 L3 压缩接线（设计稿 §12.2 / §13） ----------


async def test_l3_compaction_started_before_summarizer_then_done() -> None:
    order: list = []
    history = ScriptedCompactionHistory([l3_plan()])
    compactor = FakeCompactor(summary(), order=order)
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="最终回答")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
    )

    events = await collect(loop.run("帮我看看这篇作文", sink=OrderSink(order)))

    phases = _compaction_phases(events)
    assert phases[0] == "started"
    assert phases[-1] == "done"
    # C9 铁律：started 上屏必须早于写手请求真正发出
    assert order.index(("event", "started")) < order.index("summarize")
    done = next(e for e in events if e.type == "compaction" and e.phase == "done")
    assert done.after_tokens == 120_000
    assert done.boundary_id == "c-test123"
    # 写手收到 plan 装好的材料，commit 拿到写手正文与模型名
    assert len(compactor.calls) == 1
    assert compactor.calls[0]["n"] == 3
    assert len(history.commit_calls) == 1
    _, summary_text, model = history.commit_calls[0]
    assert summary_text == "浓缩摘要正文"
    assert model == "qwen3.7-flash"
    # 压缩失败与否都不阻断本轮：模型照常给出最终回答
    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_l3_without_compactor_skips_and_continues() -> None:
    history = ScriptedCompactionHistory([l3_plan()])
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="照常回答")]),
        ToolRegistry(),
        history=history,
    )

    events = await collect(loop.run("很长的上下文"))

    assert _compaction_phases(events) == ["skipped"]
    assert history.commit_calls == []
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


@pytest.mark.parametrize(
    "exc",
    [
        LLMError("写手网络耗尽", retryable=False),
        ContextTooLongError("材料仍超长"),
    ],
)
async def test_l3_summarizer_failure_emits_failed_and_continues(exc: Exception) -> None:
    history = ScriptedCompactionHistory([l3_plan()])
    compactor = FakeCompactor(raises=exc)
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="降级后照常回答")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
    )

    events = await collect(loop.run("很长"))

    assert _compaction_phases(events) == ["started", "failed"]
    # 写手失败绝不落 boundary
    assert history.commit_calls == []
    # 不是 run 级 FAILED：本轮拿全量原文照常发出
    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_l3_commit_failure_emits_failed_and_continues() -> None:
    history = ScriptedCompactionHistory([l3_plan()], commit_ok=False)
    compactor = FakeCompactor(summary())
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="ok")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
    )

    events = await collect(loop.run("很长"))

    phases = _compaction_phases(events)
    assert phases == ["started", "failed"]
    failed = next(e for e in events if e.type == "compaction" and e.phase == "failed")
    assert "磁盘满" in failed.message
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_l3_material_halved_emits_retrying_before_done() -> None:
    history = ScriptedCompactionHistory([l3_plan()])
    compactor = FakeCompactor(summary(material_halved=True))
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="ok")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
    )

    events = await collect(loop.run("很长"))

    assert _compaction_phases(events) == ["started", "retrying", "done"]


async def test_l2_only_plan_emits_light_l2_event_without_summarizer() -> None:
    plan = CompactionPlan(
        state="compact",
        levels=("l2",),
        predicted_tokens=160_000,
        window_tokens=300_000,
        reason="over l2",
    )
    history = ScriptedCompactionHistory([plan])
    compactor = FakeCompactor(summary())
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="ok")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
    )

    events = await collect(loop.run("hi"))

    assert _compaction_phases(events) == ["l2"]
    assert compactor.calls == []  # 纯本地装箱，不调写手
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_plan_receives_last_usage_and_300k_window() -> None:
    history = ScriptedCompactionHistory([])  # 始终 normal
    echo = ScriptedTool("echo", result={"ok": True})
    registry = ToolRegistry()
    registry.register(echo)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(
                tool_calls=[("echo", {"x": 1})], usage=Usage(input_tokens=1000, output_tokens=1234)
            ),
            FakeTurn(text="好的", usage=Usage(input_tokens=2300, output_tokens=50)),
        ]
    )
    llm.model = "glm-5.2"  # 标准档下有效窗口 = min(标称, 300k) = 300000
    loop = AgentLoop(llm, registry, history=history, retry_backoff_base=0.0)

    events = await collect(loop.run("用一下工具"))

    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
    # 两个 iteration 开头各 plan 一次
    assert len(history.plan_calls) == 2
    first, second = history.plan_calls
    assert first["window"] == 300_000
    assert first["in"] is None and first["out"] is None  # 首轮还没有真实 usage
    assert second["in"] == 1000 and second["out"] == 1234  # A2：上轮输出也回灌


class GateCompactor:
    """挂起直到被取消的写手，记录自己是否真的被掐断。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.cancelled = False

    async def summarize(
        self, *, material_records, seed_summary_text=None, material_truncated=False
    ):
        self.started.set()
        try:
            await asyncio.wait_for(self.gate.wait(), timeout=5)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return summary()  # pragma: no cover - 取消前到不了


async def test_cancel_during_summarizer_aborts_and_commits_nothing() -> None:
    history = ScriptedCompactionHistory([l3_plan()])
    compactor = GateCompactor()
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="不该出现的回答")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
    )

    task = asyncio.ensure_future(collect(loop.run("很长")))
    await asyncio.wait_for(compactor.started.wait(), timeout=2)
    loop.cancel()
    events = await asyncio.wait_for(task, timeout=2)

    assert compactor.cancelled is True
    assert history.commit_calls == []  # 取消不落 boundary
    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.CANCELLED


# ---------- §10 413 应急：主动 blocked / 被动上下文超长（先减重再重发；B5） ----------

_EMERGENCY_MARKER = "EMERGENCY_SLIMMED_VIEW_MARKER"
_B5_HINT = "这篇内容太长，一次处理不完。建议：① 把原文存成文件后用文件上传；② 分成上下两篇发给我。"


def feasible_emergency(strength: int = 0) -> EmergencyView:
    return EmergencyView(
        feasible=True,
        messages=[SystemMessage(content="s"), UserMessage(content=_EMERGENCY_MARKER)],
        estimated_tokens=80_000,
        kept_turns=1,
        protected_user_texts=(),
        strength=strength,
    )


def infeasible_emergency(strength: int, reason: str = _B5_HINT) -> EmergencyView:
    return EmergencyView(
        feasible=False,
        messages=[],
        estimated_tokens=300_000,
        kept_turns=0,
        protected_user_texts=(),
        strength=strength,
        reason=reason,
    )


class ContextTooLongThenOK:
    """前 fail_times 次 stream_chat 直接抛上下文超长，之后按剧本正常；记录每次消息。"""

    def __init__(self, fail_times: int, good_turns: list) -> None:
        self._fail_times = fail_times
        self._n = 0
        self._good = ScriptedFakeStreamingLLM(good_turns)
        self.calls: list[list] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append(list(messages))
        if self._n < self._fail_times:
            self._n += 1
            raise ContextTooLongError("context length exceeded")
            yield  # pragma: no cover - 使本函数成为 async generator
        async for event in self._good.stream_chat(
            messages, tools, max_tokens=max_tokens, strip_tools=strip_tools
        ):
            yield event


def _blob(messages) -> str:
    return "\n".join(getattr(m, "content", "") or "" for m in messages)


async def test_passive_413_slims_then_resends_and_succeeds() -> None:
    history = ScriptedCompactionHistory([], emergency_factory=lambda s: feasible_emergency(s))
    llm = ContextTooLongThenOK(1, [FakeTurn(text="救回来的回答")])
    loop = AgentLoop(llm, ToolRegistry(), history=history, retry_backoff_base=0.0)

    events = await collect(loop.run("很长的上下文"))

    assert "retrying" in _compaction_phases(events)
    assert any(isinstance(e, TurnRetryEvent) and e.reason == "context_too_long" for e in events)
    # 第一次用全量原文被拒，第二次用应急减重视图重发
    assert len(llm.calls) == 2
    assert _EMERGENCY_MARKER not in _blob(llm.calls[0])
    assert _EMERGENCY_MARKER in _blob(llm.calls[1])
    assert history.emergency_calls == [0]
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT


async def test_passive_413_both_strengths_infeasible_fails_with_hint() -> None:
    history = ScriptedCompactionHistory([], emergency_factory=lambda s: infeasible_emergency(s))
    llm = ContextTooLongThenOK(5, [FakeTurn(text="不该出现")])
    loop = AgentLoop(llm, ToolRegistry(), history=history, retry_backoff_base=0.0)

    events = await collect(loop.run("超长"))

    assert any(e.type == "compaction" and e.phase == "failed" for e in events)
    errors = [e for e in events if e.type == "error"]
    assert any("存成文件" in e.message for e in errors)
    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.FAILED
    # 不可行就不重发：模型只调一次；应急渲染从 strength0 试到 strength1
    assert len(llm.calls) == 1
    assert history.emergency_calls == [0, 1]


async def test_proactive_blocked_slims_before_first_send() -> None:
    history = ScriptedCompactionHistory(
        [l3_plan(state="blocked", levels=(), predicted_tokens=295_000)],
        emergency_factory=lambda s: feasible_emergency(s),
    )
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="精简后正常回答")])
    loop = AgentLoop(llm, ToolRegistry(), history=history)

    events = await collect(loop.run("很长"))

    assert _compaction_phases(events) == ["retrying"]
    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
    # 第一次（也是唯一一次）发送就用应急减重视图，没走写手、没被服务端拒
    assert len(llm.calls) == 1
    assert _EMERGENCY_MARKER in _blob(llm.calls[0][0])
    assert history.emergency_calls == [0]
    assert history.commit_calls == []  # 应急不调写手、不落 boundary


async def test_proactive_blocked_infeasible_fails_before_sending() -> None:
    history = ScriptedCompactionHistory(
        [l3_plan(state="blocked", levels=(), predicted_tokens=295_000)],
        emergency_factory=lambda s: infeasible_emergency(s),
    )
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="不该出现")])
    loop = AgentLoop(llm, ToolRegistry(), history=history)

    events = await collect(loop.run("超长"))

    assert any(e.type == "compaction" and e.phase == "failed" for e in events)
    errors = [e for e in events if e.type == "error"]
    assert any("存成文件" in e.message for e in errors)
    assert events[-1].type == "result"
    assert events[-1].finish_reason == FinishReason.FAILED
    assert len(llm.calls) == 0  # 组不出干净请求，一次都不发
    assert history.commit_calls == []


# ---------- [D2] 压缩遥测观察者：成功 / 写手失败都落一条样本 ----------


async def test_l3_success_observer_gets_metric_with_elapsed() -> None:
    history = ScriptedCompactionHistory([l3_plan()])
    compactor = FakeCompactor(summary())
    seen: list[CompactionMetric] = []
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="最终回答")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
        compaction_observer=seen.append,
    )

    events = await collect(loop.run("很长"))

    assert events[-1].finish_reason == FinishReason.FINAL_TEXT
    assert len(seen) == 1
    metric = seen[0]
    assert metric.success is True
    assert metric.level == "l3"
    assert metric.before_tokens == 249_000
    assert metric.after_tokens == 120_000
    assert metric.boundary_id == "c-test123"
    assert metric.elapsed_ms is not None and metric.elapsed_ms >= 0.0
    # done 事件也带上写手耗时
    done = next(e for e in events if e.type == "compaction" and e.phase == "done")
    assert done.elapsed_ms == metric.elapsed_ms


async def test_l3_summarizer_failure_observer_gets_failure_metric() -> None:
    history = ScriptedCompactionHistory([l3_plan()])
    compactor = FakeCompactor(raises=ContextTooLongError("材料仍超长"))
    seen: list[CompactionMetric] = []
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="降级后回答")]),
        ToolRegistry(),
        history=history,
        compactor=compactor,
        compaction_observer=seen.append,
    )

    events = await collect(loop.run("很长"))

    assert events[-1].finish_reason == FinishReason.FINAL_TEXT  # 降级不炸主循环
    assert len(seen) == 1
    metric = seen[0]
    assert metric.success is False
    assert metric.after_tokens is None
    assert metric.degraded_reason == "ContextTooLongError"
    assert metric.elapsed_ms is not None and metric.elapsed_ms >= 0.0


async def test_l3_without_compactor_emits_no_metric() -> None:
    # 没配写手的 skipped 是部署常态（没真正尝试、无写手耗时），不写遥测样本
    history = ScriptedCompactionHistory([l3_plan()])
    seen: list[CompactionMetric] = []
    loop = AgentLoop(
        ScriptedFakeStreamingLLM([FakeTurn(text="照常")]),
        ToolRegistry(),
        history=history,
        compaction_observer=seen.append,
    )

    events = await collect(loop.run("很长"))

    assert _compaction_phases(events) == ["skipped"]
    assert seen == []
