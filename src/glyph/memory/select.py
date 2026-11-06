"""目录页选择纯函数：谁进、怎么排、谁被挤出。

淘汰有两条规则（都是"退出目录、文件不删"）：
1. **fact 超过 N 天没被提起**；
2. **错题已掌握（mastered）**——D31 补的：用户学会了的错题不该继续占目录。
   原来只有第 1 条，导致学会的错题永远挂在目录里，挤占 50 条上限；
   MasteryStage.MASTERED 本来只做到"不再主动提醒"。

排序：最近被提起 → 次数多 → 先记下 → key（兜底确定性）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .types import MemoryEntry, MemoryType

__all__ = ["CatalogSelection", "days_since", "select_for_catalog"]

# 掌握度三档里的"已掌握"值。与 mastery.MasteryStage.MASTERED 同一字面量；
# 这里不 import mastery 是为了避免 select ← mastery ← types 的循环依赖。
MASTERED_STAGE = "mastered"


@dataclass(frozen=True)
class CatalogSelection:
    entries: list[MemoryEntry]
    stale_count: int  # 太久没被提起、退出目录的条数
    overflow_count: int  # 条数上限挤出的条数
    total_count: int


def days_since(iso_date: str, today: date) -> int | None:
    """相隔天数；日期形状非法返回 None。"""
    try:
        parsed = date.fromisoformat(iso_date)
    except ValueError:
        return None
    return (today - parsed).days


def select_for_catalog(
    entries: Sequence[MemoryEntry],
    *,
    today: date,
    max_entries: int,
    fact_unused_days: int,
    stale_types: frozenset[MemoryType] = frozenset({MemoryType.FACT}),
) -> CatalogSelection:
    survivors: list[MemoryEntry] = []
    stale_count = 0

    for entry in entries:
        # D31：错题掌握之后退出目录（文件保留——reopened_count 与"他学过什么"的
        # 历史仍有用，下次再犯能认出是复发）。只看 mistake：其他类型没有掌握度这回事。
        if entry.type is MemoryType.MISTAKE and entry.mastery_stage == MASTERED_STAGE:
            stale_count += 1
            continue
        if entry.type in stale_types:
            age = days_since(entry.last_mentioned_at, today)
            # 非法日期按最老处理：age None / 超大都会被淘汰，不让坏文件霸占目录
            if age is None or age > fact_unused_days:
                stale_count += 1
                continue
        survivors.append(entry)

    ordered = sorted(
        survivors,
        # 日期字符串 ISO 可直接字典序；空/非法值排最后（用极小占位）
        key=lambda e: (
            e.last_mentioned_at if days_since(e.last_mentioned_at, today) is not None else "",
            e.count,
            e.first_seen,
            e.key,
        ),
        reverse=True,
    )
    # key asc 是兜底确定性项：reverse=True 会把它也反了，单独再按 key 排一次
    ordered.sort(key=lambda e: e.key)
    ordered.sort(
        key=lambda e: (
            e.last_mentioned_at if days_since(e.last_mentioned_at, today) is not None else "",
            e.count,
            e.first_seen,
        ),
        reverse=True,
    )

    chosen = ordered[:max_entries]
    return CatalogSelection(
        entries=chosen,
        stale_count=stale_count,
        overflow_count=len(ordered) - len(chosen),
        total_count=len(entries),
    )
