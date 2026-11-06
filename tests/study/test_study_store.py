"""背词后端状态仓库。"""

from __future__ import annotations

from pathlib import Path

from glyph.study import StudyStore
from glyph.study.paths import state_path


def test_env_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GLYPH_STUDY_ROOT", str(tmp_path))
    assert StudyStore().path == state_path(tmp_path)


def test_get_empty_and_bad_file(tmp_path: Path) -> None:
    store = StudyStore(tmp_path)
    assert store.get() is None
    store.path.write_text("{bad", encoding="utf-8")
    assert store.get() is None


def test_put_get_roundtrip(tmp_path: Path) -> None:
    store = StudyStore(tmp_path)
    state = {"settings": {"bookId": "cet6"}, "queue": {"newWords": ["commit"]}, "cards": {}}
    store.put(state)
    assert store.get() == state
    store.clear()
    assert store.get() is None
