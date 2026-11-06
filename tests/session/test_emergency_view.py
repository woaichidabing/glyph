"""§10 emergency_view（413 应急的临时减重渲染；B5 未结束任务原文不砍）。

钉四件事：
- 只保留最近 N 个往返 + 一条应急续接 user（旧往来收起、提货单兜底），strength 越大留得越少；
- B5：未关闭 grade 任务、被切在 tail 之外的用户来稿原文，仍强制留在应急视图里；
- 单条来稿大到连"系统词 + 原文 + 1 往返"都超窗口时 feasible=False，给"存文件 / 分两篇"出路；
- 应急只改渲染、不落盘：流水一条记录都不增。
"""

from __future__ import annotations

import pytest

from glyph.session import new_session
from glyph.session.compaction_l3 import TRUST_CLOSE, TRUST_OPEN
from glyph.session.records import RecordType
from glyph.types import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    UserMessage,
)

WINDOW = 300_000
HOMEWORK = "MY_HOMEWORK_ORIGINAL " + ("作文原文需要被完整保留。" * 20)


async def _plain_turn(history, marker: str, *, size: int = 40) -> None:
    await history.record_user_message(f"{marker} " + "啊" * size)
    await history.record_assistant_message(
        AssistantMessage(blocks=[TextBlock(text=f"reply-{marker}")])
    )


async def _open_grade_turn(history, *, essay: str) -> None:
    """造一个 grade 已返回结果、但**没有 finalize 定稿**（任务仍开放）的往返。"""
    await history.record_user_message("帮我批改这篇作文")
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id="g1", name="grade_text", arguments={"text": essay})]
        )
    )
    await history.record_tool_result(
        "g1",
        ToolResult(
            tool_name="grade_text",
            ok=True,
            output={"summary": "若干语法问题", "detections": []},
        ),
    )


def _user_blob(messages) -> str:
    return "\n".join(m.content for m in messages if isinstance(m, UserMessage))


@pytest.mark.asyncio
async def test_emergency_strength0_keeps_recent_and_adds_resume(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _plain_turn(history, "OLDMARKER")
    await _plain_turn(history, "MIDMARKER")
    await _plain_turn(history, "RECENTMARKER")
    records_before = len(history._records)

    view = history.emergency_view(effective_window=WINDOW, strength=0)

    assert view.feasible is True
    blob = _user_blob(view.messages)
    assert "暂时收起" in blob  # 应急续接话术
    assert "RECENTMARKER" in blob  # 最近往返保留
    assert "OLDMARKER" not in blob  # 最旧往返被收起
    # 只改渲染、不落盘
    assert len(history._records) == records_before


@pytest.mark.asyncio
async def test_emergency_strength1_keeps_only_one_turn(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _plain_turn(history, "OLDMARKER")
    await _plain_turn(history, "MIDMARKER")
    await _plain_turn(history, "RECENTMARKER")

    v0 = history.emergency_view(effective_window=WINDOW, strength=0)
    v1 = history.emergency_view(effective_window=WINDOW, strength=1)

    assert v0.kept_turns >= v1.kept_turns
    assert v1.kept_turns == 1
    blob = _user_blob(v1.messages)
    assert "RECENTMARKER" in blob
    assert "MIDMARKER" not in blob


@pytest.mark.asyncio
async def test_emergency_protects_open_grade_essay_even_when_outside_tail(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _open_grade_turn(history, essay=HOMEWORK)
    # 之后再来两个往返：grade 后 users_after=2 < idle(3)，任务仍开放（未 finalize）
    await _plain_turn(history, "FOLLOW1")
    await _plain_turn(history, "FOLLOW2")

    view = history.emergency_view(effective_window=WINDOW, strength=1)

    assert view.feasible is True
    blob = _user_blob(view.messages)
    # B5：未结束任务的作文原文虽落在 tail（最近 1 往返）之外，仍被 trust 袋子强制带回
    assert "MY_HOMEWORK_ORIGINAL" in blob
    assert TRUST_OPEN in blob and TRUST_CLOSE in blob
    assert "作文原文需要被完整保留" in blob
    # 最近往返也还在
    assert "FOLLOW2" in blob


@pytest.mark.asyncio
async def test_emergency_single_huge_essay_infeasible_with_file_upload_hint(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    huge = "超长篇作文 " + ("学生粘贴的超长英文原文。" * 30000)
    await _open_grade_turn(history, essay=huge)
    await _plain_turn(history, "AFTER1")

    view = history.emergency_view(effective_window=WINDOW, strength=1)

    # 连下限 1 往返 + 这篇原文都超窗口 → 不可行；去掉原文就能 fit，故归因 B5 单篇超长
    assert view.feasible is False
    assert "存成文件" in view.reason
    assert "分" in view.reason  # "分成上下两篇"


@pytest.mark.asyncio
async def test_emergency_does_not_invent_compact_records(tmp_path) -> None:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await _plain_turn(history, "OLDMARKER")
    await _plain_turn(history, "RECENTMARKER")

    history.emergency_view(effective_window=WINDOW, strength=0)

    # 应急绝不落 summary / boundary 记录
    kinds = {r.record_type for r in history._records}
    assert RecordType.COMPACT_SUMMARY not in kinds
    assert RecordType.COMPACT_BOUNDARY not in kinds
