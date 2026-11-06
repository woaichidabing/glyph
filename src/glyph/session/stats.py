"""会话流水的用量统计：把跨会话的逐轮用量按天加起来（设置页「用量与花费」用）。

数据源就是流水里每轮的 ``turn_end.usage``（本来就在记），不新增埋点、不改任何东西。
三条口径：

- **按记录自己的日期归天**，不按文件时间——一次会话可能跨天；
- 老会话没记过用量就不计入，也不假装是 0：``cached_known`` 分得清"未知"与"真 0"；
- **文件级快筛**：整份文件最后改动时间早于窗口起点就跳过（流水只追加、时间单调），
  这样不必为了 7 天的账去解析全部历史。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .errors import SessionError
from .paths import iter_session_paths
from .records import Record, RecordType, TurnEndPayload
from .store import SessionStore

__all__ = ["DayUsage", "UsageSummary", "add_records", "summarize_sessions"]


@dataclass
class DayUsage:
    """一天的用量。

    ``cached_known=False`` 表示这一天一轮都没回过缓存明细（未知）——序列化时给 null，
    界面据此不显示百分比，而不是显示 0%（0 的含义是"一个都没命中"）。
    """

    day: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cached_known: bool = False
    turns: int = 0
    # 只把"确实带了缓存明细"的那些轮的输入加在这里。2026-10-12 之前的流水没记
    # cached_tokens，拿它们的输入当命中率的分母会把比例压得很难看（实测 19% vs 真值 95%）
    # —— 算比例只能用在有明细的那部分上。
    measured_input_tokens: int = 0

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens if self.cached_known else None,
            "measured_input_tokens": self.measured_input_tokens,
            "turns": self.turns,
        }


def add_records(bucket: DayUsage, records: Iterable[Record]) -> None:
    """把一段流水里所有 turn_end 的用量并进这个桶（坏记录跳过，不当成 0）。"""
    for rec in records:
        if rec.record_type is not RecordType.TURN_END:
            continue
        try:
            turn = rec.payload_as(TurnEndPayload)
        except ValidationError:
            continue
        if turn.usage is None:
            continue
        bucket.turns += 1
        bucket.input_tokens += turn.usage.input_tokens or 0
        bucket.output_tokens += turn.usage.output_tokens or 0
        if turn.usage.cached_tokens is not None:
            bucket.cached_known = True
            bucket.cached_tokens += turn.usage.cached_tokens
            bucket.measured_input_tokens += turn.usage.input_tokens or 0


@dataclass
class UsageSummary:
    """窗口内按天的用量 + 窗口合计（合计里的 cached_tokens 同样是 null = 未知）。"""

    window_days: int
    days: list[DayUsage] = field(default_factory=list)

    def to_dict(self) -> dict:
        total = DayUsage(day="total")
        for day in self.days:
            total.input_tokens += day.input_tokens
            total.output_tokens += day.output_tokens
            total.turns += day.turns
            total.measured_input_tokens += day.measured_input_tokens
            if day.cached_known:
                total.cached_known = True
                total.cached_tokens += day.cached_tokens
        return {
            "window_days": self.window_days,
            "days": [day.to_dict() for day in self.days],
            "total": total.to_dict(),
        }


def summarize_sessions(root: Path, *, days: int, today: date) -> UsageSummary:
    """最近 ``days`` 天（含今天）的用量按天加总；窗口内没有用量的天也在列表里（柱子要连续）。"""
    span = [today - timedelta(days=days - 1 - i) for i in range(days)]
    buckets = {d.isoformat(): DayUsage(day=d.isoformat()) for d in span}

    for path in iter_session_paths(root):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime).date() < span[0]:
                continue  # 快筛：整份文件都在窗口外
            records = SessionStore(path).read_records().records
        except (SessionError, OSError):
            continue
        for rec in records:
            bucket = buckets.get(str(rec.ts)[:10])
            if bucket is None:  # 窗口外的记录（或 ts 形状不对）：不计
                continue
            add_records(bucket, [rec])

    return UsageSummary(window_days=days, days=[buckets[d.isoformat()] for d in span])
