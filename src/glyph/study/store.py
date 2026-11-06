"""背词状态快照：设置 / 队列 / 卡片 / 错词 / 随心背游标。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .paths import state_path, study_root


class StudyStore:
    def __init__(self, root: Path | None = None) -> None:
        base = Path(root).expanduser() if root is not None else study_root()
        self._path = state_path(base)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> dict | None:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(raw, dict):
                return None
            state = raw.get("state")
            return state if isinstance(state, dict) else None

    def put(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise ValueError("state 必须是对象")
        payload = {"version": 1, "state": state}
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".study-", suffix=".json", dir=str(self._path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp, self._path)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def clear(self) -> None:
        with self._lock:
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass
