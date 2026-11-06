from __future__ import annotations

import json
from pathlib import Path

from glyph.e01_watch import E01Logger, SessionWatcher


def _line(seq: int, record_type: str, payload: dict, ts: str = "2026-10-02T10:00:00+08:00") -> str:
    return (
        json.dumps(
            {"v": 1, "seq": seq, "ts": ts, "type": record_type, "payload": payload},
            ensure_ascii=False,
        )
        + "\n"
    )


def _session(root: Path, session_id: str, *lines: str) -> Path:
    path = root / "2026" / "10" / "02" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_watcher_ignores_existing_sessions_but_captures_new_session(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    output = tmp_path / "e01.jsonl"
    old = _session(
        sessions,
        "session-20261002-100000-aaaaaa",
        _line(0, "session_meta", {"session_id": "session-20261002-100000-aaaaaa"}),
        _line(1, "user_message", {"text": "旧测试"}),
    )
    watcher = SessionWatcher(sessions, E01Logger(output))

    watcher.poll_once()
    assert not output.exists()

    new = _session(
        sessions,
        "session-20261002-100001-bbbbbb",
        _line(0, "session_meta", {"session_id": "session-20261002-100001-bbbbbb"}),
    )
    new.open("a", encoding="utf-8").write(_line(1, "user_message", {"text": "新测试"}))

    watcher.poll_once()
    assert [event["type"] for event in _events(output)] == ["session_meta", "user_message"]
    assert all(event["session_id"] == "session-20261002-100001-bbbbbb" for event in _events(output))
    assert old.exists()


def test_watcher_waits_for_a_complete_json_line_and_does_not_duplicate(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    output = tmp_path / "e01.jsonl"
    path = _session(
        sessions,
        "session-20261002-100002-cccccc",
        _line(0, "session_meta", {"session_id": "session-20261002-100002-cccccc"}),
    )
    watcher = SessionWatcher(sessions, E01Logger(output), from_start=True)
    watcher.poll_once()
    assert len(_events(output)) == 1

    partial = _line(1, "assistant_message", {"text": "半截"}).rstrip("\n")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[:12])
    watcher.poll_once()
    assert len(_events(output)) == 1

    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial[12:] + "\n")
    watcher.poll_once()
    watcher.poll_once()
    assert [event["type"] for event in _events(output)] == ["session_meta", "assistant_message"]
    assert _events(output)[1]["payload"]["text"] == "半截"


def test_from_start_replays_existing_session_and_preserves_payload(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    output = tmp_path / "e01.jsonl"
    _session(
        sessions,
        "session-20261002-100003-dddddd",
        _line(0, "session_meta", {"session_id": "session-20261002-100003-dddddd"}),
        _line(1, "user_message", {"text": "我只有 5 分钟"}),
        _line(2, "assistant_message", {"text": "先做一道题"}),
    )
    watcher = SessionWatcher(sessions, E01Logger(output), from_start=True)
    watcher.poll_once()

    events = _events(output)
    assert [event["type"] for event in events] == [
        "session_meta",
        "user_message",
        "assistant_message",
    ]
    assert events[1]["payload"] == {"text": "我只有 5 分钟"}
