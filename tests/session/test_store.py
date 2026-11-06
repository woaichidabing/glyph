"""session/store.py 测试：一个会话一个文件、只追加写盘（D14）。

切片 1：一行一个 JSON 对象。
切片 2：断行能修能跳。
切片 3：大 payload 整条落盘，不截断。
切片 4：并发追加按会话文件串行，不穿插不丢行。
切片 5：首行不是 session_meta 就是不可解析的会话；关了不再可写。
切片 6：扫目录列会话列表（S6）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from glyph.session import SessionHistory, new_session
from glyph.session.errors import SessionCorruptError, SessionNotFoundError, SessionWriteError
from glyph.session.records import (
    APP_VERSION,
    SCHEMA_VERSION,
    Record,
    SessionMeta,
    UserMessagePayload,
    build_record,
)
from glyph.session.store import SessionStore, iter_session_summaries, session_lock

_TZ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 9, 19, 17, 50, 3, tzinfo=_TZ)
_SYSTEM_PROMPT = "你是一位耐心的英语老师。"


def _fake_clock() -> datetime:
    """定死的时间源（R6：时钟可注入），断言里的时间戳才有确定性。"""
    return _NOW


def _session_meta_record() -> Record:
    return build_record(
        seq=0,
        ts=_NOW.isoformat(),
        payload=SessionMeta(
            schema_version=SCHEMA_VERSION,
            session_id="20260919-175003-8f3c2a",
            model="qwen-plus",
            cwd="D:\\markr",
            created_at=_NOW.isoformat(),
            system_prompt_fingerprint="sha256:1a2b3c4d5e6f",
        ),
    )


def _user_record(seq: int, text: str) -> Record:
    return build_record(seq=seq, ts=_NOW.isoformat(), payload=UserMessagePayload(text=text))


def _lines(path: Path) -> list[str]:
    """按行读文件内容，丢掉空行。"""
    return [line for line in path.read_text(encoding="utf-8").split("\n") if line]


# ---------- 切片 1：一行一个 JSON ----------


async def test_append_writes_one_json_object_per_line(tmp_path) -> None:
    """一行一个 JSON 对象：不多不少一行，写完 flush（不强 fsync）。"""
    path = tmp_path / "session.jsonl"
    store = SessionStore(path, clock=_fake_clock)

    await store.append(_session_meta_record())
    await store.append(_user_record(1, "帮我改这句：I has a apple"))

    lines = _lines(path)
    assert len(lines) == 2
    for line in lines:
        assert list(json.loads(line)) == ["v", "seq", "ts", "type", "payload"]
    assert json.loads(lines[0])["type"] == "session_meta"
    assert json.loads(lines[1])["payload"]["text"] == "帮我改这句：I has a apple"

    read = store.read_records()
    assert len(read.records) == 2
    assert read.skipped_lines == 0

    store.close()


# ---------- 切片 2：断行 ----------


async def test_append_repairs_missing_trailing_newline(tmp_path) -> None:
    """上一行被掐断没留换行：追加前先补一个 \\n，不能让新的一行粘到断行尾巴上。"""
    path = tmp_path / "session.jsonl"
    path.write_text(_session_meta_record().to_json_line(), encoding="utf-8")  # 故意不带行尾 \n
    store = SessionStore(path, clock=_fake_clock)

    await store.append(_user_record(1, "继续上一句"))

    lines = _lines(path)
    assert json.loads(lines[0])["type"] == "session_meta"
    assert json.loads(lines[1])["payload"]["text"] == "继续上一句"

    read = store.read_records()
    assert len(read.records) == 2
    assert read.skipped_lines == 0

    store.close()


def test_read_skips_torn_last_line_and_counts_it(tmp_path) -> None:
    """读时坏行整行跳过并计数：只丢它自己，前后几行照常可用。"""
    path = tmp_path / "session.jsonl"
    torn = _user_record(2, "这一句写到一半进程被强杀").to_json_line()[:42]
    path.write_text(
        "\n".join(
            [
                _session_meta_record().to_json_line(),
                _user_record(1, "帮我改这句：I has a apple").to_json_line(),
                torn,  # 断行，没有行尾换行
            ]
        ),
        encoding="utf-8",
    )

    read = SessionStore(path).read_records()

    assert [r.seq for r in read.records] == [0, 1]
    assert read.skipped_lines == 1


# ---------- 切片 3：大 payload ----------


async def test_large_payload_is_written_whole(tmp_path) -> None:
    """大 payload 整条原样写入：不截断、不摘出去单独存文件。"""
    path = tmp_path / "session.jsonl"
    long_text = "今天要改的这句英文是 I has a apple。" * 3000  # 约 5 万字符
    store = SessionStore(path, clock=_fake_clock)

    await store.append(_session_meta_record())
    await store.append(_user_record(1, long_text))

    lines = _lines(path)
    assert len(lines) == 2  # 没被拆成多行
    assert len(json.loads(lines[1])["payload"]["text"]) == len(long_text)

    restored_text = store.read_records().records[1].payload_as(UserMessagePayload).text
    assert restored_text == long_text

    store.close()


# ---------- 切片 4：并发 ----------


class _SplitWriteStore(SessionStore):
    """把一次写入拆成两半、中间让出事件循环：用来放大并发交错，检验锁是否真的串行。"""

    async def _write_line(self, line: str) -> None:
        handle = self._ensure_handle()
        handle.write(line[:12])
        handle.flush()
        await asyncio.sleep(0)
        handle.write(line[12:])
        handle.flush()


def test_same_path_shares_one_session_lock(tmp_path) -> None:
    """同一会话文件共用一把锁；不同会话各用各的，互不影响。"""
    path = tmp_path / "session.jsonl"

    assert session_lock(path) is session_lock(path)
    assert session_lock(path) is not session_lock(tmp_path / "other.jsonl")


async def test_concurrent_appends_are_serialized_by_session_lock(tmp_path) -> None:
    """两个写者对着同一个文件并发追加：一行都不会被截断、也不会丢。"""
    path = tmp_path / "session.jsonl"
    writer_a = _SplitWriteStore(path, clock=_fake_clock)
    writer_b = _SplitWriteStore(path, clock=_fake_clock)

    async def write_batch(writer: SessionStore, tag: str, seqs: range) -> None:
        for seq in seqs:
            await writer.append(_user_record(seq, f"{tag}-{seq}"))

    await asyncio.gather(
        write_batch(writer_a, "A", range(1, 5)),
        write_batch(writer_b, "B", range(5, 9)),
    )

    lines = _lines(path)
    assert len(lines) == 8  # 没丢行
    texts = []
    for line in lines:
        parsed = json.loads(line)  # 有穿插这里就会炸：一行不再是完整 JSON
        assert list(parsed) == ["v", "seq", "ts", "type", "payload"]
        texts.append(parsed["payload"]["text"])
    assert sorted(texts) == sorted(
        [f"A-{i}" for i in range(1, 5)] + [f"B-{i}" for i in range(5, 9)]
    )

    writer_a.close()
    writer_b.close()


# ---------- 切片 5：坏文件 ----------


def test_read_without_session_meta_raises_corrupt(tmp_path) -> None:
    """首行是文件的身份证：没有 session_meta 就拒绝打开。"""
    path = tmp_path / "session.jsonl"
    path.write_text(_user_record(0, "孤儿行").to_json_line() + "\n", encoding="utf-8")

    with pytest.raises(SessionCorruptError):
        SessionStore(path).read_records()


def test_read_empty_file_raises_corrupt(tmp_path) -> None:
    """空文件同样不可解析 —— 而不是被当成「一个没有历史的会话」悄悄放行。"""
    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")

    with pytest.raises(SessionCorruptError):
        SessionStore(path).read_records()


def test_read_undecodable_file_raises_corrupt(tmp_path) -> None:
    """不是 UTF-8 的会话文件：报「损坏」，绝不把 UnicodeDecodeError 裸抛出去炸接口。"""
    path = tmp_path / "session.jsonl"
    path.write_bytes("会话标题：乱码".encode("gbk"))

    with pytest.raises(SessionCorruptError):
        SessionStore(path).read_records()


def test_read_missing_file_raises_not_found(tmp_path) -> None:
    """文件压根不存在：报「找不到」，不报「损坏」，两者处理方式不同。"""
    with pytest.raises(SessionNotFoundError):
        SessionStore(tmp_path / "not-there.jsonl").read_records()


async def test_append_after_close_is_rejected(tmp_path) -> None:
    """会话关了就不许再写：与其静默少几行，不如当场报错。"""
    store = SessionStore(tmp_path / "session.jsonl", clock=_fake_clock)
    await store.append(_session_meta_record())
    store.close()

    with pytest.raises(SessionWriteError):
        await store.append(_user_record(1, "迟到的记录"))


# ---------- 切片 6：会话列表（S6）----------


def _clock_at(moment: datetime):
    """返回一个走针到指定时刻的时钟（R6 可注入）。"""

    def clock() -> datetime:
        return moment

    return clock


async def _make_session(root: Path, moment: datetime) -> SessionHistory:
    """在指定日期建一个新会话，返回 history 对象（落盘位置即该日期的分层目录）。"""
    return await new_session(
        system_prompt=_SYSTEM_PROMPT,
        model="qwen-plus",
        cwd="D:\\markr",
        root=root,
        clock=_clock_at(moment),
    )


async def test_list_sessions_returns_recent_first(tmp_path) -> None:
    """列表 = 扫目录 + 只读首行：最新在前，不建任何索引。"""
    oldest = await _make_session(tmp_path, datetime(2026, 8, 31, 9, 0, 0, tzinfo=_TZ))
    morning = await _make_session(tmp_path, datetime(2026, 9, 19, 9, 0, 0, tzinfo=_TZ))
    evening = await _make_session(tmp_path, datetime(2026, 9, 19, 17, 50, 3, tzinfo=_TZ))

    summaries = list(iter_session_summaries(tmp_path))

    assert [summary.session_id for summary in summaries] == [
        evening.session_id,
        morning.session_id,
        oldest.session_id,
    ]
    newest = summaries[0]
    assert newest.path == evening.path
    assert newest.model == "qwen-plus"
    assert newest.created_at == evening.session_meta.created_at
    assert newest.app_version == APP_VERSION
    # limit 用于「最近 N 个会话」这种展示场景
    assert [s.session_id for s in iter_session_summaries(tmp_path, limit=2)] == [
        evening.session_id,
        morning.session_id,
    ]


async def test_list_sessions_skips_unreadable_files(tmp_path) -> None:
    """一个坏文件不该让整个列表挂掉：读不出 session_meta 的都跳过。"""
    good = await _make_session(tmp_path, datetime(2026, 9, 19, 17, 50, 3, tzinfo=_TZ))
    good.close()
    day_dir = good.path.parent

    torn = day_dir / "session-20260919-120000-abcdef.jsonl"
    torn.write_text(
        '{"v":1,"seq":0,"ts":"2026-09-19T12:00:00+08:00","type":"sess', encoding="utf-8"
    )
    no_meta = day_dir / "session-20260919-130000-bbbbbb.jsonl"
    no_meta.write_text(
        build_record(0, _NOW.isoformat(), UserMessagePayload(text="缺少身份证")).to_json_line()
        + "\n",
        encoding="utf-8",
    )

    summaries = list(iter_session_summaries(tmp_path))

    assert [summary.session_id for summary in summaries] == [good.session_id]
