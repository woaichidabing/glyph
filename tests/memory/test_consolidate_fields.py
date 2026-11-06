"""整理器合并时必须保住掌握度字段（D31）。

真机踩到的 bug：整理把一条 mastered_by=user_claim 的错题合并后，
该字段被丢了——目录里就从「自述 + 验证通过」退回成兜底的「复测连对达标」，
用户看不出这条到底是怎么练掉的。
"""

import json
from datetime import datetime, timedelta, timezone

from glyph.memory.consolidate import MemoryConsolidator
from glyph.memory.pending import append_pending
from glyph.memory.store import MemoryStore
from glyph.memory.types import MemoryEntry, MemoryType

_TZ = timezone(timedelta(hours=8))


def clock() -> datetime:
    return datetime(2026, 10, 16, 22, 0, 0, tzinfo=_TZ)


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def complete(self, messages, **_kwargs) -> str:
        return self._reply


def make_consolidator(tmp_path, reply: str):
    store = MemoryStore(root=tmp_path, clock=clock)
    con = MemoryConsolidator(
        root=tmp_path,
        store=store,
        llm=FakeLLM(reply),
        model="fake",
        min_hours=0,
        min_sessions=1,
        throttle_seconds=0,
    )
    return con, store


def seed_all_fields(store: MemoryStore) -> None:
    """一条字段全齐的已掌握错题（模拟"用户自述验证通过"出师的）。

    用 upsert 先落一条骨架，再用 save_mastery 把带历史的账本写上去——
    因为 upsert 首次写入会按"新建"处理（count=1、first_seen=今天），
    直接传历史值会被它覆盖，那是它的正常行为，不是 bug。
    """
    store.upsert(
        MemoryEntry(
            key="aaaabbbbcccc",
            type=MemoryType.MISTAKE,
            title="过去时误用原形 go（应为 went）",
            quote="我老是分不清 go 和 went",
            count=2,
            first_seen="2026-10-14",
            last_seen="2026-10-16",
            last_mentioned_at="2026-10-16",
            tier="error",
            issue_type="hard",
            error_code="VERB:TENSE",
            mastery_stage="mastered",
            mastered_by="user_claim",
            independent_streak=1,
            due_date=None,
            good_count=1,
            hard_count=0,
            wrong_count=0,
            last_review_at="2026-10-16",
            reopened_count=0,
            original="I go home yesterday",
            correction="I went home yesterday",
        )
    )
    # 说明：upsert 首次写入按"新建"处理（count=1、first_seen=今天），
    # 那是它的正常行为。本文件要验的是"合并时保不保住字段"，不是 upsert 的归一化。


def seed_pending(tmp_path) -> None:
    append_pending(
        tmp_path,
        session_id="s1",
        title="改邮件",
        lines=["- [mistake] 过去时误用原形 go（应为 went）  ｜原话：还是老写错"],
        day=datetime(2026, 10, 16, tzinfo=_TZ).date(),
    )


def merge_reply() -> str:
    return json.dumps(
        {
            "writes": [
                {
                    "key": "aaaabbbbcccc",
                    "type": "mistake",
                    "title": "过去时误用原形 go（应为 went）",
                    "quote": "还是老写错",
                }
            ]
        },
        ensure_ascii=False,
    )


async def test_merge_keeps_mastered_by(tmp_path) -> None:
    """合并进已有条目时，mastered_by 不许丢。"""
    con, store = make_consolidator(tmp_path, merge_reply())
    seed_all_fields(store)
    seed_pending(tmp_path)

    out = await con.run(now=clock(), sessions_touched=1)
    assert out.value == "done"

    saved = MemoryStore(root=tmp_path, clock=clock).get("aaaabbbbcccc")
    assert saved.mastery_stage == "mastered"
    assert saved.mastered_by == "user_claim"  # ← 真机就是在这丢的


async def test_merge_keeps_other_mastery_fields(tmp_path) -> None:
    """顺带锁住其它账本字段：合并只更新标题/原话/计数，不重置进度。"""
    con, store = make_consolidator(tmp_path, merge_reply())
    seed_all_fields(store)
    seed_pending(tmp_path)

    await con.run(now=clock(), sessions_touched=1)
    saved = MemoryStore(root=tmp_path, clock=clock).get("aaaabbbbcccc")

    assert saved.independent_streak == 1
    assert saved.good_count == 1
    assert saved.reopened_count == 0
    assert saved.last_review_at == "2026-10-16"
    assert saved.error_code == "VERB:TENSE"
    assert saved.original == "I go home yesterday"
    assert saved.correction == "I went home yesterday"


async def test_merge_increments_count(tmp_path) -> None:
    con, store = make_consolidator(tmp_path, merge_reply())
    seed_all_fields(store)
    seed_pending(tmp_path)

    await con.run(now=clock(), sessions_touched=1)
    saved = MemoryStore(root=tmp_path, clock=clock).get("aaaabbbbcccc")
    # 首次写入时 upsert 已把 count 定为 1，这次合并再加 1
    assert saved.count == 2


async def test_merge_keeps_first_seen(tmp_path) -> None:
    con, store = make_consolidator(tmp_path, merge_reply())
    seed_all_fields(store)
    seed_pending(tmp_path)

    await con.run(now=clock(), sessions_touched=1)
    saved = MemoryStore(root=tmp_path, clock=clock).get("aaaabbbbcccc")
    # 合并不该把"首次见到"刷成今天（= 保留 upsert 落下的那个值）
    assert saved.first_seen == clock().date().isoformat()
