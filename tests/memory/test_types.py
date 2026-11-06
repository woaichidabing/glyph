"""memory 领域模型测试（R4 单点）。"""

import pytest

from glyph.memory.types import (
    MEMORY_VERSION,
    MemoryEntry,
    MemoryType,
    TimeHint,
    WriteAction,
)


def test_memory_type_has_exactly_four_values() -> None:
    assert {t.value for t in MemoryType} == {
        "mistake",
        "preference",
        "feedback",
        "fact",
    }


def test_memory_version_is_two() -> None:
    # v2：mistake 增加掌握度账本字段；旧 v1 文件靠字段缺省兼容、不写迁移
    assert MEMORY_VERSION == 2


def test_write_action_values() -> None:
    assert {a.value for a in WriteAction} == {"created", "updated", "unchanged"}


def test_time_hint_accepts_valid_exact_date() -> None:
    hint = TimeHint(quote="下周五", exact="2026-10-02")

    assert hint.exact == "2026-10-02"
    assert hint.approximate is False


def test_time_hint_accepts_valid_span() -> None:
    hint = TimeHint(quote="下周", span_start="2026-09-28", span_end="2026-10-04", approximate=True)

    assert hint.approximate is True


def test_time_hint_rejects_bad_date_shape() -> None:
    with pytest.raises(ValueError):
        TimeHint(quote="x", exact="10月2号")


def test_time_hint_rejects_span_end_before_start() -> None:
    with pytest.raises(ValueError):
        TimeHint(quote="x", span_start="2026-10-04", span_end="2026-09-28")


def test_minimal_memory_entry() -> None:
    entry = MemoryEntry(
        key="abc123",
        type=MemoryType.PREFERENCE,
        title="讲解用中文",
        quote="以后讲解都用中文",
        count=1,
        first_seen="2026-09-25",
        last_seen="2026-09-25",
        last_mentioned_at="2026-09-25",
        version=MEMORY_VERSION,
    )

    assert entry.tier is None
    assert entry.error_code is None
    assert entry.mastery_stage is None
    assert entry.due_date is None
    assert entry.time is None
