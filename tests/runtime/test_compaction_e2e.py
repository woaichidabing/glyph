"""#67 上下文压缩端到端（离线，不打真实 API）。

与 tests/runtime/test_compaction_loop.py 的区别：那里用脚本化的 **InMemoryHistory 替身**
钉 loop 编排；这里用**真实落盘 SessionHistory + 真实 AgentLoop**，只把"模型"和"浓缩写手"
换成脚本，验证整条链路在真账本上真的发生：

- L3：先跑一轮拿到服务端真实 usage（A2 记账），下一轮才按水位触发浓缩 → 写手 → 落
  summary/boundary → 切片视图真的发给主脑 → 重开会话切片仍在；
- D2：文件观察者把每次 L3 尝试追加一行 JSON 到磁盘；
- 被动 413：主脑第一次真抛 context_length，loop 用真实 emergency_view 减重后重发成功，
  且应急**不落任何压缩记录**（只改渲染、不删流水）。

阈值口径（标准档 W=300k）：L2 线 150k、L3 线 210k、硬线 288k。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import FakeTurn, ScriptedFakeStreamingLLM

from glyph.llm.base import TextDelta, TurnEnd
from glyph.llm.compactor import ContextTooLongError, SummaryResult
from glyph.runtime.compaction_metrics import file_compaction_observer
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.session import new_session, open_session
from glyph.session.records import RecordType
from glyph.tools.factory import build_read_artifact_tool
from glyph.types import (
    AssistantMessage,
    FinishReason,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    Usage,
    UserMessage,
)

WINDOW = 300_000
SUMMARY = "【浓缩】学习者前面练了一般现在时的八篇小作文。"


# ---------- helpers ----------


def _blob(messages) -> str:
    """把一叠消息揉成可搜索的纯文本（不关心角色，只断言某段原文在 / 不在）。"""
    parts: list[str] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            parts.append(m.content)
        elif isinstance(m, UserMessage):
            parts.append(m.content)
        elif isinstance(m, AssistantMessage):
            parts.append(m.text)
        elif isinstance(m, ToolMessage):
            parts.append(m.content)
    return "\n".join(p for p in parts if p)


class ScriptedCompactor:
    """浓缩写手替身：不联网，固定返回一段摘要，记录被调用次数与材料规模。"""

    def __init__(self, text: str = SUMMARY) -> None:
        self.text = text
        self.calls = 0
        self.material_counts: list[int] = []

    async def summarize(
        self, *, material_records, seed_summary_text=None, material_truncated=False
    ) -> SummaryResult:
        self.calls += 1
        self.material_counts.append(len(material_records))
        return SummaryResult(
            text=self.text,
            model="qwen3.7-flash",
            input_tokens=1000,
            output_tokens=200,
            material_halved=False,
            finish_reason="stop",
        )


async def _history_with_turns(tmp_path, *, rounds: int = 8, per_user_bytes: int = 85_000):
    """开一个真会话，灌入 ``rounds`` 个带可识别标记的大往返（模拟旧的多篇批改）。

    每篇 user 约 85k 字节、8 篇 ≈ 680k 字节，本地 measure 约 22 万 token：稳过 L2 线
    （15 万），压缩后不会被 C5「桌子放得下就放回原文」解压；又低于硬线（28.8 万）。
    """
    h = await new_session(
        system_prompt="你是英语教练。",
        model="glm-5.2",
        cwd=str(tmp_path),
        root=tmp_path,
    )
    # 与 loop 的标准档 effective_window 对齐（测试不依赖本机 .env 档位）。
    h._context_window_tokens = WINDOW
    for i in range(rounds):
        await h.record_user_message(f"essay-marker-{i} " + "y" * per_user_bytes)
        await h.record_assistant_message(
            AssistantMessage(blocks=[TextBlock(text=f"对第 {i} 篇的反馈。")])
        )
    return h


def _make_loop(history, llm, *, compactor=None, observer=None):
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        context_mode="standard",
        compactor=compactor,
        compaction_observer=observer,
        retry_backoff_base=0.0,
    )
    return loop


# ---------- L3 端到端：真实水位触发 → 切片发送 → 落盘 → 重开持久化 ----------


@pytest.mark.asyncio
async def test_e2e_l3_compaction_slices_then_persists(tmp_path) -> None:
    history = await _history_with_turns(tmp_path)
    llm = ScriptedFakeStreamingLLM(
        [
            # 第一轮：服务端回报当前上下文已到 245k（过 L3 线 210k、未到硬线 288k）。
            # 这一轮之前没有真实 usage，按 C1 只做 L1、不主动压，正常发出。
            FakeTurn(text="预热回答", usage=Usage(input_tokens=245_000, output_tokens=1_000)),
            # 第二轮：压缩切片后真正发给主脑的那一次。
            FakeTurn(text="压缩后回答", usage=Usage(input_tokens=120_000, output_tokens=500)),
        ]
    )
    llm.model = "glm-5.2"
    compactor = ScriptedCompactor()
    metrics: list = []
    loop = _make_loop(history, llm, compactor=compactor, observer=metrics.append)

    try:
        first = [e async for e in loop.run("预热问题")]
        assert first[-1].finish_reason == FinishReason.FINAL_TEXT
        # 首轮没有真实 usage，不主动压缩。
        assert [e for e in first if e.type == "compaction"] == []

        second = [e async for e in loop.run("接着上一篇继续")]
        assert second[-1].finish_reason == FinishReason.FINAL_TEXT
        assert second[-1].text == "压缩后回答"

        # 事件序列：started 必须早于 done（C9：写手慢时用户先看到"正在浓缩"）。
        comp = [e for e in second if e.type == "compaction"]
        phases = [e.phase for e in comp]
        assert "started" in phases and "done" in phases
        assert phases.index("started") < phases.index("done")
        assert not any(p in ("failed", "skipped") for p in phases)
        assert compactor.calls == 1  # 一轮最多一次 L3

        # 主脑被调两次：第一次看到全量（含最旧一篇），第二次只收到切片后的视图。
        assert len(llm.calls) == 2
        first_sent, second_sent = llm.calls[0][0], llm.calls[1][0]
        assert "essay-marker-0" in _blob(first_sent)
        sliced = _blob(second_sent)
        assert "essay-marker-0" not in sliced  # 最旧原文已移出 body
        assert SUMMARY in sliced  # 续接渲染了浓缩摘要
        assert "essay-marker-7" in sliced  # 最近 tail 原文保住

        # 账本真落了 summary + boundary（原文记录一条没删，只是渲染切片）。
        types_ = [r.record_type for r in history._records]
        assert RecordType.COMPACT_SUMMARY in types_
        assert RecordType.COMPACT_BOUNDARY in types_

        # D2：一次成功 L3 遥测，压后水位明显低于压前。
        assert len(metrics) == 1
        metric = metrics[0]
        assert metric.success is True and metric.level == "l3"
        assert metric.after_tokens is not None
        assert metric.after_tokens < metric.before_tokens
        assert metric.elapsed_ms is not None and metric.elapsed_ms >= 0.0
        # [D2 构成账] 成功 L3 的遥测带上压前窗口七桶估算（会话以用户大往返为主）。
        assert metric.composition is not None
        assert set(metric.composition) == {
            "system",
            "context",
            "user",
            "assistant",
            "tool_call",
            "artifact",
            "scaffolding",
        }
        assert metric.composition["user"] > 0
        assert metric.composition["system"] > 0
        session_id = history.session_id
    finally:
        history.close()

    # 重开会话：切片视图持久化（最旧原文不在 body、摘要仍在；原文记录仍在磁盘可调阅）。
    reopened = await open_session(session_id, system_prompt="你是英语教练。", root=tmp_path)
    try:
        reopened_blob = _blob(reopened.messages())
        assert "essay-marker-0" not in reopened_blob
        assert SUMMARY in reopened_blob
        assert "essay-marker-7" in reopened_blob
    finally:
        reopened.close()


# ---------- D2 端到端：文件观察者真把 L3 尝试追加落盘 ----------


@pytest.mark.asyncio
async def test_e2e_l3_appends_metric_jsonl_to_disk(tmp_path) -> None:
    history = await _history_with_turns(tmp_path)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(text="ok1", usage=Usage(input_tokens=245_000, output_tokens=1_000)),
            FakeTurn(text="ok2", usage=Usage(input_tokens=120_000, output_tokens=500)),
        ]
    )
    llm.model = "glm-5.2"
    metrics_path = tmp_path / "compaction.jsonl"
    loop = _make_loop(
        history,
        llm,
        compactor=ScriptedCompactor(),
        observer=file_compaction_observer(metrics_path),
    )

    try:
        await _drain(loop.run("预热"))
        await _drain(loop.run("再聊"))
        assert metrics_path.exists()
        lines = [ln for ln in metrics_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        row = json.loads(lines[0])
        for key in (
            "ts",
            "success",
            "level",
            "boundary_id",
            "before_tokens",
            "after_tokens",
            "elapsed_ms",
            "material_truncated",
            "degraded_reason",
            "composition",
        ):
            assert key in row
        assert row["success"] is True and row["level"] == "l3"
        assert row["boundary_id"]
        # [D2 构成账] 七桶构成随 jsonl 落盘，用户桶为正。
        assert isinstance(row["composition"], dict)
        assert row["composition"].get("user", 0) > 0
    finally:
        history.close()


async def _drain(run) -> list:
    return [event async for event in run]


# ---------- 被动 413 端到端：真实 emergency_view 减重后重发成功，不落压缩记录 ----------


class FirstCallContextTooLong:
    """第一次 stream_chat 真抛 context_length，第二次起正常回答。"""

    def __init__(self) -> None:
        self.model = "glm-5.2"
        self.calls: list[list] = []

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            raise ContextTooLongError("This model's maximum context length is exceeded.")
        await asyncio.sleep(0)
        yield TextDelta(delta="应急减重后的回答")
        yield TurnEnd(stop_reason="stop", usage=Usage(input_tokens=80_000, output_tokens=200))


@pytest.mark.asyncio
async def test_e2e_passive_413_slims_with_real_emergency_view_without_records(tmp_path) -> None:
    history = await _history_with_turns(tmp_path, rounds=8, per_user_bytes=40_000)
    llm = FirstCallContextTooLong()
    # 被动应急不依赖浓缩写手（§10：救命只做 tail + 提货单，不再调第二次 L3 模型）。
    loop = _make_loop(history, llm, compactor=None)

    try:
        events = await _drain(loop.run("这一轮发送时主脑报超长"))

        assert events[-1].finish_reason == FinishReason.FINAL_TEXT
        assert events[-1].text == "应急减重后的回答"

        # 一次 413 → 一次应急减重重试（turn_retry 带 context_too_long 原因）。
        retries = [e for e in events if e.type == "turn_retry"]
        assert len(retries) == 1
        assert retries[0].reason == "context_too_long"
        emergency = [e for e in events if e.type == "compaction" and e.level == "emergency"]
        assert emergency and emergency[0].phase == "retrying"

        # 主脑恰好被调两次：第一次全量被拒，第二次收到减重视图（最旧原文移出）。
        assert len(llm.calls) == 2
        assert "essay-marker-0" not in _blob(llm.calls[1])
        assert "essay-marker-7" in _blob(llm.calls[1])

        # 铁律：应急只改渲染，绝不落 summary / boundary，流水一条不删。
        types_ = [r.record_type for r in history._records]
        assert RecordType.COMPACT_SUMMARY not in types_
        assert RecordType.COMPACT_BOUNDARY not in types_
    finally:
        history.close()


# ---------- L2 装箱 → read_artifact 取回全链路（压不得清单的核心承诺） ----------


GRADE_CALL = "grade_call_1"
GRADE_SOURCE = "GRADE-SOURCE-ESSAY-42"
GRADE_DETAIL = "VERIFY-DETAIL-777 第三处一般现在时主语是 he，动词漏了 s"


@pytest.mark.asyncio
async def test_e2e_l2_packs_old_grade_then_read_artifact_recovers_it(tmp_path) -> None:
    """旧批改成品在 L2 装箱后从 body 收成提货单，但 read_artifact 能取回全文并回灌。"""
    history = await new_session(
        system_prompt="你是英语教练。",
        model="glm-5.2",
        cwd=str(tmp_path),
        root=tmp_path,
    )
    history._context_window_tokens = WINDOW

    # 最早一篇真实批改：grade_text 工具调用（含作文原文）+ 成品结果 + 一句收尾。
    essay = GRADE_SOURCE + " " + "e" * 2000
    await history.record_user_message("帮我批改这篇：" + essay)
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id=GRADE_CALL, name="grade_text", arguments={"text": essay})]
        )
    )
    await history.record_tool_result(
        GRADE_CALL,
        ToolResult(
            tool_name="grade_text",
            ok=True,
            output={
                "summary": "VERIFY-SUMMARY-777 总体不错，一处时态问题。",
                "detections": [{"id": "d1", "message": GRADE_DETAIL}],
            },
        ),
    )
    await history.record_assistant_message(
        AssistantMessage(blocks=[TextBlock(text="批改好了，共 1 处问题。")])
    )

    # 之后 7 个大往返：让该任务 idle 关闭、移出最近 3 轮保护窗，并把水位顶过 L2 线（150k）。
    for i in range(7):
        await history.record_user_message(f"pack-marker-{i} " + "z" * 85_000)
        await history.record_assistant_message(
            AssistantMessage(blocks=[TextBlock(text=f"后续对话 {i}")])
        )

    # 注册真实 read_artifact 工具（history_provider 指向当前会话）。
    registry = ToolRegistry()
    registry.register(build_read_artifact_tool(history_provider=lambda: history))

    llm = ScriptedFakeStreamingLLM(
        [
            # run1：服务端回报 180k（过 L2 线 150k、未到 L3 线 210k），首轮只做 L1。
            FakeTurn(text="预热", usage=Usage(input_tokens=180_000, output_tokens=500)),
            # run2：L2 装箱后，主脑看到提货单 → 调 read_artifact 取回最旧那篇。
            FakeTurn(
                tool_calls=[("read_artifact", {"call_id": GRADE_CALL})],
                usage=Usage(input_tokens=180_000, output_tokens=300),
            ),
            # 取回结果回灌后，主脑基于全文细节作答。
            FakeTurn(
                text="已为你调回那处时态细节", usage=Usage(input_tokens=182_000, output_tokens=400)
            ),
        ]
    )
    llm.model = "glm-5.2"
    loop = AgentLoop(
        llm,
        registry,
        history=history,
        context_mode="standard",
        retry_backoff_base=0.0,
    )

    try:
        await _drain(loop.run("预热问题"))
        events = await _drain(loop.run("我最早那篇作文第三处问题是什么来着？"))
        assert events[-1].finish_reason == FinishReason.FINAL_TEXT

        # run2 触发的是 L2 装箱（不是 L3，也没失败）。
        comp = [e for e in events if e.type == "compaction"]
        assert any(e.level == "l2" for e in comp)
        assert not any(e.level == "l3" for e in comp)

        # calls[1] = run2 装箱后第一次发给主脑：成品详情已折叠，逐条细节不在 body。
        assert len(llm.calls) >= 3
        packed_view = _blob(llm.calls[1][0])
        assert GRADE_DETAIL not in packed_view

        # read_artifact 真的从全量流水取回了成品全文。
        recovered = history.read_artifact(GRADE_CALL)
        assert recovered is not None
        assert GRADE_DETAIL in str(recovered["detections"])
        assert GRADE_SOURCE in recovered["source_text"]

        # 取回结果在下一次发给主脑时回到了模型眼前（B3：取回正在讲解的内容完整保留）。
        after_fetch = _blob(llm.calls[2][0])
        assert GRADE_DETAIL in after_fetch
    finally:
        history.close()
