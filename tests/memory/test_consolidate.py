"""整理器测试（D29）：低频合并、有锁、失败回滚、读不出来就跳过。

用假模型，全部离线（R6）。
"""

import json
from datetime import datetime, timedelta, timezone

from glyph.memory.consolidate import ConsolidateOutcome, MemoryConsolidator
from glyph.memory.pending import append_pending, list_pending
from glyph.memory.store import MemoryStore

_TZ = timezone(timedelta(hours=8))



def clock() -> datetime:
    return datetime(2026, 10, 15, 22, 0, 0, tzinfo=_TZ)



class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[object]] = []

    async def complete(self, messages, **_kwargs) -> str:
        self.calls.append(list(messages))
        return self._replies.pop(0) if self._replies else json.dumps({"writes": []})



def writes(*items: dict) -> str:
    return json.dumps({"writes": list(items)}, ensure_ascii=False)



def make_consolidator(tmp_path, replies, **overrides):
    llm = FakeLLM(replies)
    store = MemoryStore(root=tmp_path, clock=clock)
    con = MemoryConsolidator(
        root=tmp_path,
        store=store,
        llm=llm,
        model="qwen3.7-flash",
        min_hours=overrides.pop("min_hours", 24),
        min_sessions=overrides.pop("min_sessions", 3),
        throttle_seconds=overrides.pop("throttle_seconds", 600),
        **overrides,
    )
    return con, llm, store



def seed_pending(tmp_path, n: int = 1):
    for i in range(n):
        append_pending(
            tmp_path,
            session_id=f"s{i}",
            title=f"会话{i}",
            lines=[f"> 第{i}条材料"],
            day=datetime(2026, 10, 15, tzinfo=_TZ).date(),
        )



# ---- 触发门槛 ----


async def test_skips_when_too_soon(tmp_path) -> None:
    """距上次整理不到 min_hours → 不跑（省 token）。"""
    con, llm, _ = make_consolidator(tmp_path, [writes()], min_hours=24)
    seed_pending(tmp_path)
    con.mark_consolidated(at=datetime(2026, 10, 15, 20, 0, tzinfo=_TZ))

    out = await con.run(now=datetime(2026, 10, 15, 22, 0, tzinfo=_TZ), sessions_touched=5)
    assert out == ConsolidateOutcome.SKIP_TOO_SOON
    assert llm.calls == []



async def test_skips_when_too_few_sessions(tmp_path) -> None:
    con, llm, _ = make_consolidator(tmp_path, [writes()], min_sessions=3)
    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=2)
    assert out == ConsolidateOutcome.SKIP_TOO_FEW_SESSIONS
    assert llm.calls == []



async def test_skips_when_nothing_pending(tmp_path) -> None:
    con, llm, _ = make_consolidator(tmp_path, [writes()])
    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)
    assert out == ConsolidateOutcome.SKIP_NOTHING_PENDING
    assert llm.calls == []



async def test_skips_within_throttle_window(tmp_path) -> None:
    con, llm, _ = make_consolidator(tmp_path, [writes()], throttle_seconds=600)
    seed_pending(tmp_path)
    con.mark_consolidated(at=datetime(2026, 10, 14, 22, 0, tzinfo=_TZ))
    con.mark_scanned(at=datetime(2026, 10, 16, 21, 59, tzinfo=_TZ))
    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)
    assert out == ConsolidateOutcome.SKIP_THROTTLED
    assert llm.calls == []



# ---- 锁 ----


async def test_skips_when_lock_held(tmp_path) -> None:
    """别的进程正在整理 → 不跑，防互相覆盖。"""
    con, llm, _ = make_consolidator(tmp_path, [writes()])
    seed_pending(tmp_path)
    (tmp_path / ".consolidate.lock").write_text("999999", encoding="utf-8")

    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)
    assert out == ConsolidateOutcome.SKIP_LOCKED
    assert llm.calls == []



async def test_releases_lock_after_run(tmp_path) -> None:
    con, _, _ = make_consolidator(tmp_path, [writes()])
    seed_pending(tmp_path)
    await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)
    assert not (tmp_path / ".consolidate.lock").exists()



# ---- 正常合并 ----


async def test_merges_and_clears_pending(tmp_path) -> None:
    item = {"key": "aaaaaaaaaaaa", "type": "preference", "title": "讲解用中文", "quote": "说中文"}
    con, llm, store = make_consolidator(tmp_path, [writes(item)])
    seed_pending(tmp_path, 2)

    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    assert out == ConsolidateOutcome.DONE
    assert len(llm.calls) == 1
    assert store.get("aaaaaaaaaaaa").title == "讲解用中文"
    assert list_pending(tmp_path) == []  # 处理完就收走



async def test_prompt_includes_pending_and_existing(tmp_path) -> None:
    item = {"key": "aaaaaaaaaaaa", "type": "fact", "title": "12月14日考四级", "quote": "12月考"}
    con, llm, store = make_consolidator(tmp_path, [writes(item)])
    seed_pending(tmp_path)
    await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    prompt = str(llm.calls[0])
    assert "第0条材料" in prompt  # 带上了暂存内容
    assert "MEMORY.md" in prompt or "现有" in prompt  # 也带了现有记忆



async def test_prompt_carries_four_phase_structure(tmp_path) -> None:
    con, llm, _ = make_consolidator(tmp_path, [writes()])
    seed_pending(tmp_path)
    await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    prompt = str(llm.calls[0])
    assert "相对日期" in prompt  # 把"昨天"转成具体日期
    assert "矛盾" in prompt  # 两个文件打架修错的那个



# ---- 失败回滚 + 推迟下次 ----


async def test_bad_reply_does_not_clear_pending(tmp_path) -> None:
    """模型回复坏了 → 不合并、不清暂存，下次重看。"""
    con, _, store = make_consolidator(tmp_path, ["不是json"])
    seed_pending(tmp_path)
    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    assert out == ConsolidateOutcome.FAILED
    assert len(list_pending(tmp_path)) == 1  # 材料还在



async def test_failure_delays_next_trigger(tmp_path) -> None:
    con, _, _ = make_consolidator(tmp_path, ["坏"])
    seed_pending(tmp_path)
    await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    # 失败后立刻再来一次：因为推迟了，应该是 SKIP_TOO_SOON 而不是重试
    out = await con.run(now=datetime(2026, 10, 16, 22, 1, tzinfo=_TZ), sessions_touched=5)
    assert out == ConsolidateOutcome.SKIP_TOO_SOON



async def test_llm_error_rolls_back_and_keeps_pending(tmp_path) -> None:
    class Boom:
        async def complete(self, messages, **_kwargs) -> str:
            raise RuntimeError("炸了")


    store = MemoryStore(root=tmp_path, clock=clock)
    con = MemoryConsolidator(
        root=tmp_path, store=store, llm=Boom(), model="m",
        min_hours=24, min_sessions=3, throttle_seconds=600,
    )
    seed_pending(tmp_path)
    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    assert out == ConsolidateOutcome.FAILED
    assert len(list_pending(tmp_path)) == 1



# ---- 记忆读不出来 → 跳过整次并说明原因 ----


async def test_unreadable_memory_skips_whole_run(tmp_path) -> None:
    """抄 CC：读不出来不拿脏数据乱改，并告诉用户为什么、什么时候恢复。"""
    con, llm, _ = make_consolidator(tmp_path, [writes()])
    seed_pending(tmp_path)
    con.mark_memory_unreadable(reason="权限不足")

    out = await con.run(now=datetime(2026, 10, 16, 22, 0, tzinfo=_TZ), sessions_touched=5)

    assert out == ConsolidateOutcome.SKIP_UNREADABLE
    assert llm.calls == []
    assert "权限不足" in con.last_notice()



def test_outcome_enum_is_stable() -> None:
    expected = {
        "DONE", "FAILED", "SKIP_TOO_SOON", "SKIP_TOO_FEW_SESSIONS",
        "SKIP_NOTHING_PENDING", "SKIP_THROTTLED", "SKIP_LOCKED", "SKIP_UNREADABLE",
    }
    assert {o.name for o in ConsolidateOutcome} == expected
