"""目录页淘汰 / 排序 / 挤出纯函数测试。"""

from datetime import date

from glyph.memory.select import days_since, select_for_catalog
from glyph.memory.types import MemoryEntry, MemoryType

_TODAY = date(2026, 9, 25)


def entry(
    key: str,
    type_: MemoryType = MemoryType.PREFERENCE,
    *,
    last_mentioned: str = "2026-09-20",
    count: int = 1,
    first_seen: str = "2026-09-01",
    title: str = "x",
) -> MemoryEntry:
    return MemoryEntry(
        key=key,
        type=type_,
        title=title,
        quote="q",
        count=count,
        first_seen=first_seen,
        last_seen=last_mentioned,
        last_mentioned_at=last_mentioned,
    )


def test_days_since_valid_and_invalid() -> None:
    assert days_since("2026-09-20", _TODAY) == 5
    assert days_since("坏日期", _TODAY) is None


def test_fact_unused_beyond_window_exits_but_file_counted() -> None:
    selection = select_for_catalog(
        [entry("a", MemoryType.FACT, last_mentioned="2026-08-20")],  # 36 天
        today=_TODAY,
        max_entries=50,
        fact_unused_days=30,
    )

    assert selection.entries == []
    assert selection.stale_count == 1
    assert selection.total_count == 1


def test_fact_exactly_at_window_stays() -> None:
    selection = select_for_catalog(
        [entry("a", MemoryType.FACT, last_mentioned="2026-08-26")],  # 正好 30 天
        today=_TODAY,
        max_entries=50,
        fact_unused_days=30,
    )

    assert [e.key for e in selection.entries] == ["a"]
    assert selection.stale_count == 0


def test_mistake_never_times_out_even_a_year_old() -> None:
    selection = select_for_catalog(
        [entry("a", MemoryType.MISTAKE, last_mentioned="2025-09-25")],
        today=_TODAY,
        max_entries=50,
        fact_unused_days=30,
    )

    assert [e.key for e in selection.entries] == ["a"]


def test_sorting_recent_then_count_then_first_seen() -> None:
    selection = select_for_catalog(
        [
            entry("old_recent", last_mentioned="2026-09-01", first_seen="2026-01-01"),
            entry("new_recent", last_mentioned="2026-09-20", first_seen="2026-09-01"),
            entry("tie_highcount", last_mentioned="2026-09-20", count=5),
        ],
        today=_TODAY,
        max_entries=50,
        fact_unused_days=30,
    )

    assert [e.key for e in selection.entries] == [
        "tie_highcount",
        "new_recent",
        "old_recent",
    ]


def test_overflow_truncates_and_counts() -> None:
    selection = select_for_catalog(
        [entry(f"k{i:02d}") for i in range(5)],
        today=_TODAY,
        max_entries=2,
        fact_unused_days=30,
    )

    assert len(selection.entries) == 2
    assert selection.overflow_count == 3


def test_invalid_last_mentioned_fact_is_treated_oldest_and_exits() -> None:
    selection = select_for_catalog(
        [entry("bad", MemoryType.FACT, last_mentioned="not-a-date")],
        today=_TODAY,
        max_entries=50,
        fact_unused_days=30,
    )

    assert selection.entries == []
    assert selection.stale_count == 1
