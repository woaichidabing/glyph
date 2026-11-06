"""会话文件的只追加读写（D14 的落盘细节全在这一个文件里）。

上层（``history.py``）只认「追加一行 / 读回全部」两件事，不关心文件在哪、怎么加锁、
断行怎么修。写盘策略：

- **长持一个追加句柄**：会话期间 ``mode="a"`` 打开一次，既不 truncate 也不 seek 回写；
- **每行写完 flush**（把 Python 缓冲交给 OS），**不 fsync**：本地个人版丢最后几行可接受，
  且坏行可跳、会话仍可续；
- **追加前补换行**：上次写一半被掐断时文件末尾没有 ``\\n``，先把那一行收尾，别让新行粘上去；
- **读时跳坏行**：坏行整行跳过并计数，未知 ``type`` 整行忽略（向前兼容）；
- **大 payload 整条写**：不截断、不抽出去单独存；
- **并发**：进程内按会话文件路径共用一把 ``asyncio.Lock``，防止两处交错写出行内穿插。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, ValidationError

from .errors import SessionCorruptError, SessionNotFoundError, SessionWriteError
from .paths import iter_session_paths, local_now
from .records import Record, RecordType, SessionMeta
from .replay import ReplayResult, build_replay, extract_last_input_tokens, extract_session_usage

_LOCKS: dict[str, asyncio.Lock] = {}


def session_lock(path: Path) -> asyncio.Lock:
    """取这个会话文件对应的锁：同一路径共用一把，不同路径互不影响。

    锁常驻不删：一个进程下来开的会话数量有限，而半路回收会让正在排队的写者拿到新锁，
    反而丢掉串行保证。跨进程互斥是另一个话题，本地个人版先不做。
    """
    key = str(path)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


@dataclass(frozen=True)
class ReadResult:
    """读回结果：记录列表 + 被跳过的行数。

    跳过多少一定要告诉调用方 —— 静默丢数据比报错更难排查。
    """

    records: list[Record]
    skipped_lines: int


class SessionSummary(BaseModel):
    """会话列表用的一条摘要：只读每个文件的首行就够，不必读全文。"""

    session_id: str
    path: Path
    created_at: str
    model: str
    app_version: str = ""
    system_prompt_fingerprint: str = ""


def iter_session_summaries(
    root: Path | None = None,
    *,
    limit: int | None = None,
) -> Iterator[SessionSummary]:
    """列出会话摘要（最新在前）：按目录遍历文件，**每个文件只读首行**。

    不建索引（D15）：会话数量有限，索引完全可以从 JSONL 重建，多一份就多一处漂移。
    读不出 session_meta 的文件（空文件 / 断在第一行 / 没有身份证）一律跳过 —— 一个坏
    文件不该让整个列表挂掉。
    """
    yielded = 0
    for path in iter_session_paths(root):
        summary = read_session_summary(path)
        if summary is None:
            continue
        yield summary
        yielded += 1
        if limit is not None and yielded >= limit:
            return


def read_session_summary(path: Path) -> SessionSummary | None:
    """读一个会话文件的首行 meta；读不出来返回 None（由调用方决定怎么处理）。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except (OSError, UnicodeDecodeError):
        # 读不了 / 不是 UTF-8：整个文件跳过，列表里少一条比列表挂掉好
        return None
    if not first_line:
        return None
    try:
        record = Record.model_validate_json(first_line)
    except (json.JSONDecodeError, ValidationError):
        return None
    if record.record_type is not RecordType.SESSION_META:
        return None

    meta = record.payload_as(SessionMeta)
    return SessionSummary(
        session_id=meta.session_id,
        path=path,
        created_at=meta.created_at,
        model=meta.model,
        app_version=meta.app_version,
        system_prompt_fingerprint=meta.system_prompt_fingerprint,
    )


class SessionStore:
    """一个会话 JSONL 文件的读写器：只追加、不改写、绝不 seek 回写。"""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = local_now) -> None:
        self._path = path
        self._clock = clock
        self._handle: TextIO | None = None
        self._closed = False

    @property
    def path(self) -> Path:
        """本会话文件的绝对路径（给人排查用）。"""
        return self._path

    async def append(self, record: Record) -> None:
        """追加一行。整行序列化 + 写入 + flush 都在会话锁里完成，不会被别的写者插队。"""
        async with session_lock(self._path):
            await self._write_line(record.to_json_line() + "\n")

    async def _write_line(self, line: str) -> None:
        """真正的一次写入。调用方必须已持有本文件的会话锁。"""
        handle = self._ensure_handle()
        try:
            handle.write(line)
            handle.flush()
        except OSError as exc:
            raise SessionWriteError(f"追加会话流水失败：{self._path}") from exc

    def read_replay(self) -> ReplayResult:
        """读回"界面能直接画"的回放序列（含工具结果）。

        与 ``read_records`` 的区别：那条给的是原始流水（给重建上下文用），
        这条给的是回放视图（给界面回灌用），**工具结果也在里面**——
        历史会话的批改面板就是靠它恢复的。

        ``skipped_lines`` 一并带出来：回放是给人看历史的通道，
        少了几条他看不出来，静默丢数据比报错更难排查。
        """
        read = self.read_records()
        return ReplayResult(
            items=build_replay(read.records),
            skipped_lines=read.skipped_lines,
            last_input_tokens=extract_last_input_tokens(read.records),
            usage=extract_session_usage(read.records),
        )

    def read_records(self) -> ReadResult:
        """读回全部记录。

        坏行整行跳过并计数；未知 ``type`` 整行忽略；首行不是 ``session_meta`` 视为文件
        整体不可解析（首行是文件的身份证，没有它拒绝打开）。
        """
        if not self._path.exists():
            raise SessionNotFoundError(f"会话文件不存在：{self._path}")

        records: list[Record] = []
        skipped = 0
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 文件不是 UTF-8 也算损坏：统一转成 SessionCorruptError，
            # 不让 UnicodeDecodeError 裸着炸到接口层（那会变成无 detail 的 500）
            raise SessionCorruptError(f"读会话文件失败：{self._path}") from exc

        for raw_line in raw_text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue  # 空行不算坏行
            try:
                record = Record.model_validate_json(line)
            except (json.JSONDecodeError, ValidationError):
                skipped += 1  # torn line：只丢它自己
                continue
            if record.record_type is None:
                continue  # 未知类型：本版不认识，整行忽略（向前兼容）
            records.append(record)

        if not records or records[0].record_type is not RecordType.SESSION_META:
            raise SessionCorruptError(f"会话文件缺少首行 session_meta：{self._path}")
        return ReadResult(records=records, skipped_lines=skipped)

    def close(self) -> None:
        """flush 并关闭句柄。关了就不再可写，再 append 会直接报错（不许静默少几行）。"""
        self._closed = True
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._handle = None

    def _ensure_handle(self) -> TextIO:
        """拿到追加句柄；第一次拿之前先修理上一次留下的断行。"""
        if self._closed:
            raise SessionWriteError(f"会话已关闭，不能再写入：{self._path}")
        if self._handle is None or self._handle.closed:
            self._repair_trailing_newline()
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._handle = self._path.open("a", encoding="utf-8", newline="\n")
            except OSError as exc:
                raise SessionWriteError(f"打开会话文件失败：{self._path}") from exc
        return self._handle

    def _repair_trailing_newline(self) -> None:
        """文件末尾不是换行就补一个：免得新行粘到上一行的断尾巴上。

        只在打开句柄时做一次 —— 同一个句柄里我们写的每行都以 ``\\n`` 收尾，不会复发。
        """
        try:
            size = self._path.stat().st_size if self._path.exists() else 0
            if size == 0:
                return
            with self._path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                last_byte = handle.read(1)
        except OSError as exc:
            raise SessionWriteError(f"检查会话文件末尾失败：{self._path}") from exc

        if last_byte in (b"\n", b""):
            return
        try:
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("\n")
        except OSError as exc:
            raise SessionWriteError(f"修复会话文件断行失败：{self._path}") from exc
