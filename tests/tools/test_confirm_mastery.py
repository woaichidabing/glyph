"""用户自述「我会了」→ 出题验证 → 已掌握（D31）的工具测试。

这条路与到期复测是两回事：
- 到期复测：注入里有到期区块（带历史错句），判档走 record_verdict，掌握记 streak；
- 自述掌握：用户自己说会了，模型先 read_memory 取错句、出一道题，答对才算掌握，记 user_claim。
"""

from datetime import datetime, timedelta, timezone

import pytest

from glyph.memory.mastery import MasteryStage
from glyph.memory.store import MemoryStore
from glyph.memory.types import MemoryEntry, MemoryType
from glyph.runtime.errors import ToolError
from glyph.tools.memory_tools import ConfirmMasteryTool

_TZ = timezone(timedelta(hours=8))



def clock() -> datetime:
    return datetime(2026, 10, 16, 20, 0, 0, tzinfo=_TZ)



def make_mistake(key: str = "aaaaaaaaaaaa", *, stage: str = "learning") -> MemoryEntry:
    streak = 1 if stage == "learning" else 0
    return MemoryEntry(
        key=key,
        type=MemoryType.MISTAKE,
        title="过去时误用原形",
        quote="我老是分不清 go 和 went",
        count=2,
        first_seen="2026-10-10",
        last_seen="2026-10-15",
        last_mentioned_at="2026-10-15",
        tier="error",
        issue_type="hard",
        error_code="VERB:TENSE",
        mastery_stage=stage,
        independent_streak=streak,
        due_date="2026-10-17",
        good_count=1,
        hard_count=0,
        wrong_count=0,
        last_review_at=None,
        reopened_count=0,
        original="I go home yesterday",
        correction="I went home yesterday",
    )



# ---- 答对 → 已掌握（记 user_claim）----


async def test_correct_answer_masters_the_point(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake())
    tool = ConfirmMasteryTool(store)

    out = await tool.run(
        {
            "key": "aaaaaaaaaaaa",
            "question": "She ___ (go) to Beijing last week.",
            "answer": "went",
            "correct": True,
        }
    )

    assert out["mastered"] is True
    assert out["mastered_by"] == "user_claim"
    assert out["due_date"] is None  # 掌握之后不再排复测



async def test_correct_answer_persists_to_disk(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake())
    await ConfirmMasteryTool(store).run(
        {"key": "aaaaaaaaaaaa", "question": "q", "answer": "a", "correct": True}
    )

    saved = MemoryStore(root=tmp_path, clock=clock).get("aaaaaaaaaaaa")
    assert saved.mastery_stage == "mastered"
    assert saved.mastered_by == "user_claim"
    assert saved.due_date is None



async def test_user_claim_is_recorded_in_review_log(tmp_path) -> None:
    """留一条流水，以后回看能分清这条是"自述已掌握"还是"连对掌握"。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake())
    await ConfirmMasteryTool(store).run(
        {
            "key": "aaaaaaaaaaaa",
            "question": "She ___ (go) to Beijing last week.",
            "answer": "went",
            "correct": True,
        }
    )

    log = store.list_reviews("aaaaaaaaaaaa")
    assert len(log) == 1
    assert log[0]["verdict"] == "user_claim"
    assert log[0]["answer"] == "went"



# ---- 答错 → 保持原状，阶梯继续 ----


async def test_wrong_answer_keeps_state(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake())
    tool = ConfirmMasteryTool(store)

    out = await tool.run(
        {
            "key": "aaaaaaaaaaaa",
            "question": "She ___ (go) to Beijing last week.",
            "answer": "go",
            "correct": False,
            "explanation": "last week 是过去时间，要用 went。",
        }
    )

    assert out["mastered"] is False
    assert out["note"]  # 要有话安慰 / 说明，不能默默不吭声
    saved = MemoryStore(root=tmp_path, clock=clock).get("aaaaaaaaaaaa")
    assert saved.mastery_stage != "mastered"  # 没已掌握
    assert saved.due_date is not None  # 复测还在



async def test_wrong_answer_does_not_advance_streak(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake())
    before = store.get("aaaaaaaaaaaa").independent_streak
    await ConfirmMasteryTool(store).run(
        {"key": "aaaaaaaaaaaa", "question": "q", "answer": "wrong", "correct": False}
    )

    after = MemoryStore(root=tmp_path, clock=clock).get("aaaaaaaaaaaa").independent_streak
    assert after == before



# ---- 拒收情形 ----


async def test_bad_key_rejected(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    with pytest.raises(ToolError):
        await ConfirmMasteryTool(store).run(
            {"key": "not-a-key", "question": "q", "answer": "a", "correct": True}
        )



async def test_unknown_key_rejected(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    with pytest.raises(ToolError):
        await ConfirmMasteryTool(store).run(
            {"key": "bbbbbbbbbbbb", "question": "q", "answer": "a", "correct": True}
        )



async def test_entry_without_mastery_cannot_be_confirmed(tmp_path) -> None:
    """没建档的普通错题（v1）没有掌握度，不能走这条路。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(
        MemoryEntry(
            key="cccccccccccc",
            type=MemoryType.MISTAKE,
            title="没建档的错题",
            quote="q",
            count=1,
            first_seen="2026-10-10",
            last_seen="2026-10-10",
            last_mentioned_at="2026-10-10",
            tier="error",
        )
    )
    with pytest.raises(ToolError):
        await ConfirmMasteryTool(store).run(
            {"key": "cccccccccccc", "question": "q", "answer": "a", "correct": True}
        )



async def test_empty_question_rejected(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake())
    with pytest.raises(ToolError):
        await ConfirmMasteryTool(store).run(
            {"key": "aaaaaaaaaaaa", "question": "  ", "answer": "a", "correct": True}
        )



async def test_already_mastered_is_reported_not_silent(tmp_path) -> None:
    """已经掌握了再确认一次：如实说"已经是了"，不报错也不重复记账。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_mistake(stage="mastered"))
    out = await ConfirmMasteryTool(store).run(
        {"key": "aaaaaaaaaaaa", "question": "q", "answer": "a", "correct": True}
    )

    assert out["mastered"] is True
    assert "已经" in out["note"] or "无需" in out["note"]



# ---- 工具身份：必须是写入工具（R10）----


def test_tool_is_a_write_tool() -> None:
    """它改掌握度账本，按 R10 必须串行执行。"""
    store = MemoryStore(root=__import__("tempfile").mkdtemp(), clock=clock)
    tool = ConfirmMasteryTool(store)
    assert tool.readonly is False
    assert tool.name == "confirm_mastery"

