"""本地 E01 实验监听器。

只读 ``~/.markr/sessions`` 下的会话 JSONL，按追加事件写入独立的 E01 日志。
默认忽略监听器启动前已经存在的会话，避免把旧测试混进当前实验；新会话从首行开始，
既有会话只读取启动后的新增行。监听器不修改 Glyph 会话流水，也不读取本聊天。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .session.paths import sessions_root
from .session.records import Record, RecordType

DEFAULT_OUTPUT = Path.home() / ".markr" / "e01" / "events.jsonl"
"""E01 监听日志的默认路径。"""


@dataclass
class _Tail:
    """一个会话文件的字节游标，保留尚未写完的最后一行。"""

    offset: int = 0
    partial: bytes = b""
    session_id: str | None = None

    def read_records(self, path: Path) -> list[Record]:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0
            self.partial = b""
            self.session_id = None
        try:
            with path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return []
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop() or b""
        records: list[Record] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = Record.model_validate_json(raw)
            except (UnicodeDecodeError, ValueError):
                continue
            if record.record_type is RecordType.SESSION_META:
                self.session_id = str(record.payload.get("session_id") or "") or None
            records.append(record)
        return records


class E01Logger:
    """把会话事件逐行写入 UTF-8 JSONL，并立即 flush。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    def append(self, *, source: Path, session_id: str, record: Record) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._handle is None or self._handle.closed:
            self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        event = {
            "source": str(source),
            "session_id": session_id,
            "seq": record.seq,
            "ts": record.ts,
            "type": record.type,
            "payload": record.payload,
        }
        self._handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> E01Logger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class SessionWatcher:
    """轮询会话目录并把新增完整记录送入日志。"""

    root: Path = field(default_factory=sessions_root)
    logger: E01Logger = field(default_factory=lambda: E01Logger(DEFAULT_OUTPUT))
    from_start: bool = False
    session_id: str | None = None
    _tails: dict[Path, _Tail] = field(default_factory=dict, init=False)
    _known_paths: set[Path] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        paths = set(self._paths())
        if not self.from_start:
            self._known_paths = paths
            for path in paths:
                try:
                    self._tails[path] = _Tail(offset=path.stat().st_size)
                except OSError:
                    self._tails[path] = _Tail()

    def _paths(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        paths = sorted(self.root.rglob("*.jsonl"))
        if self.session_id is not None:
            paths = [path for path in paths if path.stem == self.session_id]
        return paths

    def poll_once(self) -> int:
        """读取一次新增内容，返回写入的事件数。"""
        written = 0
        paths = set(self._paths())
        for path in sorted(paths):
            tail = self._tails.get(path)
            if tail is None:
                tail = _Tail(
                    offset=0
                    if self.from_start or path not in self._known_paths
                    else path.stat().st_size
                )
                self._tails[path] = tail
            records = tail.read_records(path)
            sid = tail.session_id or path.stem
            for record in records:
                if record.record_type is RecordType.SESSION_META:
                    sid = str(record.payload.get("session_id") or sid)
                    tail.session_id = sid
                self.logger.append(source=path, session_id=sid, record=record)
                written += 1
        self._known_paths.update(paths)
        return written

    def run(self, *, interval: float = 0.5) -> None:
        """持续轮询，按 Ctrl+C 退出并关闭日志文件。"""
        try:
            while True:
                self.poll_once()
                time.sleep(interval)
        except KeyboardInterrupt:
            return
        finally:
            self.logger.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="监听 Glyph 会话并记录 E01 实验事件")
    parser.add_argument("--root", type=Path, default=sessions_root(), help="会话 JSONL 根目录")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="E01 JSONL 日志路径")
    parser.add_argument("--session-id", help="只监听一个指定会话")
    parser.add_argument("--from-start", action="store_true", help="读取已存在会话的全部内容")
    parser.add_argument("--once", action="store_true", help="只扫描一次后退出")
    parser.add_argument("--interval", type=float, default=0.5, help="轮询间隔（秒）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.interval <= 0:
        raise SystemExit("--interval 必须大于 0")
    watcher = SessionWatcher(
        root=args.root,
        logger=E01Logger(args.output),
        from_start=args.from_start,
        session_id=args.session_id,
    )
    if args.once:
        try:
            watcher.poll_once()
        finally:
            watcher.logger.close()
    else:
        watcher.run(interval=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
