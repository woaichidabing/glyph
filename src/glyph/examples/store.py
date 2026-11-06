"""本地例句缓存：一个 JSON 文件，按小写单词索引。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .paths import example_root, state_path
from .types import Example


class ExampleStore:
    def __init__(self, root: Path | None = None) -> None:
        base = Path(root).expanduser() if root is not None else example_root()
        self._path = state_path(base)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _sense(pos: str, zh: str) -> str:
        return "|".join(x.strip() for x in (pos, zh) if x.strip())

    @staticmethod
    def _key(word: str, sense: str) -> str:
        word = word.strip().lower()
        return word + ("|" + sense if sense else "")

    def _load(self) -> dict[str, Example]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, list):
            return {}
        out: dict[str, Example] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            ex = Example.from_dict(item)
            if ex.word and ex.en and ex.zh:
                out[self._key(ex.word, ex.sense)] = ex
        return out

    def get(self, word: str, *, pos: str = "", zh: str = "") -> Example | None:
        key = self._key(word, self._sense(pos, zh))
        if not key:
            return None
        with self._lock:
            items = self._load()
            found = items.get(key)
            if found is not None:
                return found
            # 没给义项时保留旧调用方式：返回这个单词的任意一条缓存。
            if not pos and not zh:
                prefix = word.strip().lower() + "|"
                for k, ex in items.items():
                    if k == word.strip().lower() or k.startswith(prefix):
                        return ex
            return None

    def _write(self, items: dict[str, Example]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [items[k].to_dict() for k in sorted(items)]
        fd, tmp = tempfile.mkstemp(prefix=".examples-", suffix=".json", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, self._path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def put(self, example: Example) -> None:
        key = self._key(example.word, example.sense)
        if not key:
            return
        with self._lock:
            items = self._load()
            items[key] = example
            self._write(items)

    def delete(self, word: str) -> None:
        target = word.strip().lower()
        if not target:
            return
        with self._lock:
            items = self._load()
            keep = {k: ex for k, ex in items.items() if ex.word.strip().lower() != target}
            if len(keep) != len(items):
                self._write(keep)
