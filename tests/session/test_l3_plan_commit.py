"""L3 plan_compaction / commit_compaction（方案 §9.3 / §9.4 / §9.6 / §12.2 / §12.4，v2.1 #65）。

plan 是**纯本地、不碰网络**的编排：判级 → 选动态 tail → 装写手材料（二次压缩带上一条
摘要做种子）；commit 只做"摘要 + 边界"两条记录的原子落账。这里钉：
- normal / 仅 L2 / L3 / blocked 四态产出；
- L3 切点、tail 保留下限、材料预算与截断标志；
- commit 后 messages() 真切片、续接只渲染最后摘要、重开持久化；
- 二次压缩材料只含增量、带上一条摘要种子、新切点不回退。
"""

from __future__ import annotations

import pytest

from glyph.session import new_session, open_session
from glyph.session.compaction import estimate_tokens_from_bytes, measure
from glyph.session.records import (
    CompactBoundaryPayload,
    RecordType,
    SummaryPayload,
    UserMessagePayload,
)
from glyph.types import AssistantMessage, TextBlock, UserMessage

WINDOW = 300_000
TURN_BYTES = 120_000
SUMMARY1 = "第一次压缩的摘要：学习者在练三单。"
SUMMARY2 = "第二次压缩的摘要：学习者开始练过去时。"


async def _add_turn(history, index: int, *, size: int = TURN_BYTES) -> None:
    await history.record_user_message(f"user-marker-{index} " + "x" * size)
    await history.record_assistant_message(
        AssistantMessage(blocks=[TextBlock(text=f"reply-{index}")])
    )


def _user_markers(records) -> list[str]:
    markers: list[str] = []
    for record in records:
        if record.record_type is RecordType.USER_MESSAGE:
            text = record.payload_as(UserMessagePayload).text
            markers.append(text.split(" ")[0])
    return markers


def _all_user_text(messages) -> str:
    return "\n".join(m.content for m in messages if isinstance(m, UserMessage))


# ---------- 判级三态 + blocked ----------


@pytest.mark.asyncio
async def test_plan_normal_does_nothing(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _add_turn(history, 0, size=100)

    plan = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=100,
        last_output_tokens=100,
        estimated_tokens=500,
    )

    assert plan.state == "normal"
    assert plan.levels == ()
    assert plan.at_seq is None
    assert history._l2_packed is False


@pytest.mark.asyncio
async def test_plan_l2_only_marks_packed_but_no_cut(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _add_turn(history, 0, size=100)

    plan = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=178_000,
        last_output_tokens=1000,
        estimated_tokens=180_000,  # 过 L2 线 150k、未到 L3 线 210k
    )

    assert plan.state == "compact"
    assert "l2" in plan.levels
    assert "l3" not in plan.levels
    assert plan.at_seq is None
    assert plan.material_records == ()
    assert history._l2_packed is True


@pytest.mark.asyncio
async def test_plan_blocked_has_no_cut(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _add_turn(history, 0, size=100)

    plan = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=294_000,
        last_output_tokens=1000,
        estimated_tokens=295_000,  # 过硬线 288k
    )

    assert plan.state == "blocked"
    assert plan.at_seq is None
    assert plan.material_records == ()


# ---------- L3 切点 / tail / 材料 ----------


@pytest.mark.asyncio
async def test_plan_l3_selects_tail_and_material(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    for i in range(8):
        await _add_turn(history, i)

    plan = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=248_000,
        last_output_tokens=1000,
        estimated_tokens=250_000,  # 过 L3 线
    )

    assert plan.state == "compact"
    assert "l3" in plan.levels
    assert plan.at_seq is not None
    assert plan.boundary_id
    # tail 至少保住 min_turns（默认 2）个最新往返，且含最新一篇、不含最旧一篇。
    tail_markers = _user_markers(plan.tail_records)
    assert len(tail_markers) >= 2
    assert "user-marker-7" in tail_markers
    assert "user-marker-0" not in tail_markers
    # 材料装切点之前的旧往返：含最旧一篇、不含最新一篇。
    material_markers = _user_markers(plan.material_records)
    assert "user-marker-0" in material_markers
    assert "user-marker-7" not in material_markers
    # 首次压缩没有上一条摘要种子。
    assert plan.seed_summary_text is None
    # 材料不超过写手窗口 × 材料占比的字节预算。
    material_bytes = sum(len(r.model_dump_json()) for r in plan.material_records)
    budget = int(
        history._compact_model_window * history._compact_material_ratio * history._bytes_per_token
    )
    assert material_bytes <= budget


@pytest.mark.asyncio
async def test_plan_l3_carries_pre_compaction_composition(tmp_path) -> None:
    """过 L3 线的计划要顺带带上「压前窗口由哪类内容构成」的估算 token 账（内部遥测）。"""
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    for i in range(8):
        await _add_turn(history, i)

    plan = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=248_000,
        last_output_tokens=1000,
        estimated_tokens=250_000,  # 过 L3 线
    )
    assert "l3" in plan.levels

    comp = plan.composition
    assert set(comp) == {
        "system",
        "context",
        "user",
        "assistant",
        "tool_call",
        "artifact",
        "scaffolding",
    }
    assert all(v >= 0 for v in comp.values())
    # system_prompt="sys" 落进系统桶；8 个大往返里用户正文是绝对大头（首条 user 归 context）。
    assert comp["system"] > 0
    assert comp["user"] > comp["assistant"]

    # 构成要覆盖整个压前窗口：逐桶 ceil 之和不低于整窗一次换算（独立观测点，防漏桶）。
    whole = estimate_tokens_from_bytes(measure(history.messages(include_system=True)).total_bytes)
    assert sum(comp.values()) >= whole


# ---------- commit 落账 + 切片视图 + 持久化 ----------


@pytest.mark.asyncio
async def test_commit_writes_summary_and_boundary_and_slices(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    for i in range(8):
        await _add_turn(history, i)
    plan = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=248_000,
        last_output_tokens=1000,
        estimated_tokens=250_000,
    )

    report = await history.commit_compaction(plan, SUMMARY1, model="qwen3.7-flash")

    assert report.success is True
    assert report.level == "l3"
    assert report.at_seq == plan.at_seq
    assert report.after_tokens is not None and report.after_tokens < report.before_tokens

    # 流水末尾两条：先 summary 后 boundary，指针互相对上。
    records = history._records
    assert records[-2].record_type is RecordType.COMPACT_SUMMARY
    assert records[-1].record_type is RecordType.COMPACT_BOUNDARY
    boundary_payload = records[-1].payload_as(CompactBoundaryPayload)
    summary_payload = records[-2].payload_as(SummaryPayload)
    assert boundary_payload.at_seq == plan.at_seq
    assert boundary_payload.summary_record_seq == records[-2].seq
    assert summary_payload.covered_up_to_seq == plan.at_seq

    # 视图真切片：最旧原文消失、最新 tail 保留、续接渲染摘要。
    user_text = _all_user_text(history.messages())
    assert "user-marker-0" not in user_text
    assert "user-marker-7" in user_text
    assert SUMMARY1 in user_text

    # 重开持久化：切片视图依旧。
    reopened = await open_session(history.session_id, system_prompt="sys", root=tmp_path)
    reopened_text = _all_user_text(reopened.messages())
    assert "user-marker-0" not in reopened_text
    assert "user-marker-7" in reopened_text
    assert SUMMARY1 in reopened_text


# ---------- 二次压缩：增量材料 + 摘要种子 + 只渲染最后摘要 ----------


@pytest.mark.asyncio
async def test_second_compaction_uses_incremental_material_and_seed(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    for i in range(8):
        await _add_turn(history, i)
    plan1 = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=248_000,
        last_output_tokens=1000,
        estimated_tokens=250_000,
    )
    await history.commit_compaction(plan1, SUMMARY1)

    # 压缩后又聊了 6 个大往返，再次触线。
    for i in range(8, 14):
        await _add_turn(history, i)
    plan2 = history.plan_compaction(
        effective_window=WINDOW,
        last_input_tokens=248_000,
        last_output_tokens=1000,
        estimated_tokens=250_000,
    )

    assert "l3" in plan2.levels
    assert plan2.at_seq is not None and plan2.at_seq >= (plan1.at_seq or -1)
    # 材料种子是上一条摘要；材料只含上次 boundary 之后的增量旧往返，不含远古 u0。
    assert plan2.seed_summary_text == SUMMARY1
    tail1_markers = set(_user_markers(plan1.tail_records))
    material_markers = _user_markers(plan2.material_records)
    assert "user-marker-0" not in material_markers  # 远古内容不回流进新材料
    assert "user-marker-13" not in material_markers  # 最新一篇仍在 tail
    # 上次 tail 里至少有一篇这次被切进材料（摘要随对话增量向前滚，而非只压更早的）。
    rolled = [m for m in material_markers if m in tail1_markers]
    assert rolled, "第二次压缩的材料应包含第一次 tail 中已变旧的往返"

    await history.commit_compaction(plan2, SUMMARY2)
    user_text = _all_user_text(history.messages())
    # 续接只渲染最后一条摘要。
    assert SUMMARY2 in user_text
    assert SUMMARY1 not in user_text
    # 远古 u0、以及这次从上次 tail 切进材料的篇，正文都已不在 body；最新 tail 在。
    assert "user-marker-0" not in user_text
    for marker in rolled:
        assert marker not in user_text
    assert "user-marker-13" in user_text
