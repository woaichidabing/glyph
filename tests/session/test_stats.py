"""会话流水按天的用量统计（设置页「用量与花费」的数据来源）。

为什么要它：用户只能看见"这一轮省了多少"，看不见"这个月花了多少"。
数据本来就在流水里（每轮 turn_end 都记了用量），这里只做加法。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from glyph.session.paths import session_path
from glyph.session.records import (
    SCHEMA_VERSION,
    Record,
    SessionMeta,
    TurnEndPayload,
    UserMessagePayload,
    build_record,
)
from glyph.session.stats import DayUsage, add_records, summarize_sessions
from glyph.session.store import SessionStore
from glyph.types import FinishReason, Usage

_TZ = timezone(timedelta(hours=8))
_TODAY = date(2026, 10, 12)


def _ts(day: str, hh: int = 10) -> str:
    return datetime.fromisoformat(f"{day}T{hh:02d}:00:00").replace(tzinfo=_TZ).isoformat()


def _turn(seq: int, day: str, *, inp: int, out: int, cached: int | None, hh: int = 10) -> Record:
    return build_record(
        seq=seq,
        ts=_ts(day, hh),
        payload=TurnEndPayload(
            stop_reason=FinishReason.FINAL_TEXT,
            duration_ms=1.0,
            usage=Usage(input_tokens=inp, output_tokens=out, cached_tokens=cached),
        ),
    )


def _user(seq: int, day: str, text: str = "问一句") -> Record:
    return build_record(
        seq=seq,
        ts=_ts(day),
        payload=UserMessagePayload(text=text),
    )


# ---------- 加总（纯逻辑，不碰盘） ----------


def test_add_records_sums_turn_end_usage_only() -> None:
    bucket = DayUsage(day="2026-10-12")
    add_records(
        bucket,
        [
            _user(1, "2026-10-12"),  # 不是 turn_end：不参与
            _turn(2, "2026-10-12", inp=15033, out=106, cached=14336),
            _turn(3, "2026-10-12", inp=30399, out=165, cached=28672),
        ],
    )

    assert bucket.turns == 2
    assert bucket.input_tokens == 45432
    assert bucket.output_tokens == 271
    assert bucket.cached_tokens == 43008
    assert bucket.cached_known is True


def test_add_records_keeps_cached_unknown() -> None:
    """一轮都没回缓存明细 → cached_known 仍是 False，序列化给 null（不谎报 0）。"""
    bucket = DayUsage(day="2026-10-12")
    add_records(bucket, [_turn(1, "2026-10-12", inp=100, out=5, cached=None)])

    assert bucket.cached_known is False
    assert bucket.to_dict()["cached_tokens"] is None


def test_add_records_keeps_measured_input_separate() -> None:
    """没带缓存明细的那几轮，输入不计进"命中率的分母"。

    2026-10-12 之前的流水只记了输入、没记 cached_tokens；混在一起算比例会把 95% 压成
    19%（真机实测），所以分母只认有明细的那部分。
    """
    bucket = DayUsage(day="2026-10-12")
    add_records(
        bucket,
        [
            _turn(1, "2026-10-12", inp=300000, out=1000, cached=None),  # 老流水：只有输入
            _turn(2, "2026-10-12", inp=15033, out=106, cached=14336),  # 有明细
        ],
    )

    assert bucket.input_tokens == 315033
    assert bucket.measured_input_tokens == 15033
    assert bucket.to_dict()["measured_input_tokens"] == 15033


def test_add_records_counts_explicit_zero_as_known() -> None:
    bucket = DayUsage(day="2026-10-12")
    add_records(bucket, [_turn(1, "2026-10-12", inp=100, out=5, cached=0)])

    assert bucket.cached_known is True
    assert bucket.to_dict()["cached_tokens"] == 0


# ---------- 跨文件、跨天（走盘） ----------


async def _write_session(tmp_path: Path, session_id: str, records: list[Record]) -> Path:
    path = session_path(session_id, root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SessionStore(path)
    meta = build_record(
        seq=0,
        ts=_ts("2026-10-10"),
        payload=SessionMeta(
            session_id=session_id,
            created_at=_ts("2026-10-10"),
            model="deepseek-v4.1-flash",
            system_prompt_fingerprint="sha256:test",
            app_version="0.0.0",
            schema_version=SCHEMA_VERSION,
            cwd=str(tmp_path),
        ),
    )
    await store.append(meta)
    for rec in records:
        await store.append(rec)
    store.close()
    return path


async def test_summarize_groups_by_record_day_and_fills_empty_days(tmp_path: Path) -> None:
    await _write_session(
        tmp_path,
        "session-20261011-100000-aaaaaa",
        [
            _turn(1, "2026-10-11", inp=15000, out=100, cached=14000),
            _turn(2, "2026-10-12", inp=20000, out=200, cached=18000),
        ],
    )

    summary = summarize_sessions(tmp_path, days=3, today=_TODAY)
    by_day = {d.day: d for d in summary.days}

    assert list(by_day) == ["2026-10-10", "2026-10-11", "2026-10-12"]
    assert by_day["2026-10-11"].input_tokens == 15000
    assert by_day["2026-10-12"].input_tokens == 20000
    assert by_day["2026-10-10"].turns == 0  # 空天也在，柱子才连续
    assert summary.to_dict()["total"]["input_tokens"] == 35000


async def test_summarize_skips_files_older_than_window(tmp_path: Path) -> None:
    old = await _write_session(
        tmp_path,
        "session-20260901-100000-bbbbbb",
        [_turn(1, "2026-09-01", inp=999, out=9, cached=0)],
    )
    # 把文件时间改到窗口之前，模拟"整份都在窗口外"（快筛路径）
    past = time.time() - 40 * 86400
    import os

    os.utime(old, (past, past))

    summary = summarize_sessions(tmp_path, days=3, today=_TODAY)

    assert summary.to_dict()["total"]["input_tokens"] == 0


async def test_summarize_never_invents_zero_for_unmeasured_days(tmp_path: Path) -> None:
    """窗口内确实没有用量 → 每天都算 0 轮；合计的 cached_tokens 是 null（未知）。"""
    summary = summarize_sessions(tmp_path, days=2, today=_TODAY)

    payload = summary.to_dict()
    assert payload["total"]["turns"] == 0
    assert payload["total"]["cached_tokens"] is None
