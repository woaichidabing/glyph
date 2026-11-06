"""长期记忆领域模型单点（R4）。

- ``MemoryType``：四类记忆（mistake/preference/feedback/fact），字面量即落盘值；
- ``WriteAction``：写入结果（created/updated/unchanged），告诉模型这次写发生了什么；
- ``TimeHint``：fact 类的时间信息，构造即校验（坏日期 / 区间倒挂直接拒绝）；
- ``MemoryEntry``：一条记忆的完整内存形态，对应磁盘上一个 .md 文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "MEMORY_VERSION",
    "MemoryType",
    "WriteAction",
    "TimeHint",
    "MemoryEntry",
]

MEMORY_VERSION = 2
"""记忆文件格式版本。v2 在 mistake 上增加掌握度账本字段（未建档即缺省，旧 v1
文件照常读取、不会被自动拉进复测）；旧文件缺字段按默认值兼容，不写迁移脚本。"""


class MemoryType(StrEnum):
    MISTAKE = "mistake"
    PREFERENCE = "preference"
    FEEDBACK = "feedback"
    FACT = "fact"


class WriteAction(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def _validate_iso_date(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"日期必须是 YYYY-MM-DD：{value!r}") from exc
    # 堵住 2026-1-2、20260925 这种 strptime 放行但不是规范形状的输入
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"日期必须是 YYYY-MM-DD：{value!r}")


@dataclass(frozen=True)
class TimeHint:
    """fact 类的时间信息。两种形态二选一：精确日期 / 模糊区间。"""

    quote: str
    exact: str | None = None
    span_start: str | None = None
    span_end: str | None = None
    approximate: bool = False

    def __post_init__(self) -> None:
        if self.exact is not None:
            if self.span_start is not None or self.span_end is not None:
                raise ValueError("exact 与 span 不能同时给")
            _validate_iso_date(self.exact)
            return
        if self.span_start is None and self.span_end is None:
            raise ValueError("必须给 exact 或 span_start/span_end 之一")
        if self.span_start is None or self.span_end is None:
            raise ValueError("span_start 与 span_end 必须成对给")
        _validate_iso_date(self.span_start)
        _validate_iso_date(self.span_end)
        if self.span_end < self.span_start:
            raise ValueError("span_end 不能早于 span_start")


@dataclass
class MemoryEntry:
    """一条记忆的完整内存形态。必填字段对应 frontmatter 必填项。"""

    key: str
    type: MemoryType
    title: str
    quote: str
    count: int
    first_seen: str
    last_seen: str
    last_mentioned_at: str
    version: int = MEMORY_VERSION
    # mistake 专属
    tier: str | None = None  # error | suggestion（要改 / 可改可不改）
    issue_type: str | None = None  # 批改粗桶 hard | chinglish | wordy
    error_code: str | None = None  # ERRANT 知识点代码（账本主键），如 VERB:TENSE
    original: str | None = None
    correction: str | None = None
    explanation: str | None = None
    # 掌握度账本（v2；仅已建档的 mistake 有值，未建档 / v1 旧记忆为 None）
    mastery_stage: str | None = None  # new | learning | mastered
    independent_streak: int | None = None  # 当前连续独立做对次数
    due_date: str | None = None  # ISO 日期；掌握之后为 None
    good_count: int | None = None  # 累计独立做对
    hard_count: int | None = None  # 累计提示后对
    wrong_count: int | None = None  # 累计没做对
    last_review_at: str | None = None  # 最近一次复测日（ISO）
    reopened_count: int | None = None  # 掌握之后又犯、被复活的次数
    # D31：怎么出的师——区分"复测连对达标"和"用户自述 + 出一道题验证通过"。
    # 两种都算数，但以后回看时能分清这条依据有多硬。
    mastered_by: str | None = None  # streak | user_claim
    # feedback 专属
    why: str | None = None
    how_to_apply: str | None = None
    # fact 专属
    time: TimeHint | None = None
