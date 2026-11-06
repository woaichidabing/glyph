"""冲突消解测试（D31）：学会了的要腾地方、两条打架怎么判。

第一块（掌握后退出目录）是真 bug 的修复：原来 mistake 永不按时间淘汰，
用户学会的错题会永远挂在目录里，挤占 50 条上限。
"""

from datetime import date
from glyph.memory.select import select_for_catalog
from glyph.memory.types import MemoryEntry, MemoryType

_TODAY = date(2026, 10, 15)



def entry(key: str, *, stage: str | None = None, kind: MemoryType = MemoryType.MISTAKE):
    return MemoryEntry(
        key=key,
        type=kind,
        title=f"条目{key[-2:]}",
        quote="q",
        count=1,
        first_seen="2026-10-01",
        last_seen="2026-10-01",
        last_mentioned_at="2026-10-01",
        mastery_stage=stage,
    )



def pick(entries, **overrides):
    kwargs = dict(today=_TODAY, max_entries=50, fact_unused_days=30)
    kwargs.update(overrides)
    return select_for_catalog(entries, **kwargs)



# ---- 掌握后退出目录（D31 第①条）----


def test_mastered_mistake_leaves_catalog() -> None:
    """已掌握的错题不再进目录——腾地方给新的真弱点。"""
    entries = [entry("aaaaaaaaaaaa", stage="mastered"), entry("bbbbbbbbbbbb", stage="learning")]
    result = pick(entries)

    keys = [e.key for e in result.entries]
    assert "bbbbbbbbbbbb" in keys
    assert "aaaaaaaaaaaa" not in keys



def test_mastered_counts_as_retired_not_overflow() -> None:
    """退出原因是"已掌握"，不是"被挤出去"——统计要分得清。"""
    entries = [entry("aaaaaaaaaaaa", stage="mastered")]
    result = pick(entries)

    assert result.entries == []
    assert result.stale_count == 1  # 与 fact 超期同一类：退出目录
    assert result.overflow_count == 0



def test_mastered_file_is_not_deleted() -> None:
    """只退出目录，条数还在 total_count 里——文件没删（历史要留）。"""
    entries = [entry("aaaaaaaaaaaa", stage="mastered")]
    result = pick(entries)
    assert result.total_count == 1



def test_learning_and_new_stay_in_catalog() -> None:
    entries = [
        entry("aaaaaaaaaaaa", stage="new"),
        entry("bbbbbbbbbbbb", stage="learning"),
        entry("cccccccccccc", stage=None),
    ]
    keys = {e.key for e in pick(entries).entries}
    assert keys == {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"}



def test_mastered_preference_is_not_retired() -> None:
    """只有错题会因为已掌握而退出；偏好类的"stage"不适用，照常留着。"""
    entries = [
        MemoryEntry(
            key="aaaaaaaaaaaa",
            type=MemoryType.PREFERENCE,
            title="讲解用中文",
            quote="说中文",
            count=1,
            first_seen="2026-10-01",
            last_seen="2026-10-01",
            last_mentioned_at="2026-10-01",
            mastery_stage="mastered",  # 理论上不会有；真出现了也不该被吞掉
        )
    ]
    assert [e.key for e in pick(entries).entries] == ["aaaaaaaaaaaa"]



def test_fact_still_expires_by_time() -> None:
    """原有规则不许被这次改动碰坏：fact 超期照旧退出目录。"""
    old = MemoryEntry(
        key="aaaaaaaaaaaa",
        type=MemoryType.FACT,
        title="旧事实",
        quote="q",
        count=1,
        first_seen="2026-01-01",
        last_seen="2026-01-01",
        last_mentioned_at="2026-01-01",  # 远超 30 天
    )
    result = pick([old])
    assert result.entries == []
    assert result.stale_count == 1



def test_retired_count_is_reported() -> None:
    """退出目录的条数要能被读到（界面 / 日志用）。"""
    entries = [
        entry("aaaaaaaaaaaa", stage="mastered"),
        entry("bbbbbbbbbbbb", stage="mastered"),
        entry("cccccccccccc", stage="learning"),
    ]
    result = pick(entries)
    assert result.stale_count == 2
    assert len(result.entries) == 1

