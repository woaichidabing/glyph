import pytest

from glyph import storage


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GLYPH_DATA_ROOT", str(tmp_path / "custom"))
    assert storage.data_root() == tmp_path / "custom"


def test_pointer_wins_over_legacy(tmp_path, monkeypatch):
    pointer = tmp_path / "pointer.json"
    target = tmp_path / "D-GlyphData"
    target.mkdir()
    monkeypatch.setattr(storage, "pointer_path", lambda: pointer)
    monkeypatch.setattr(storage, "legacy_data_root", lambda: tmp_path / "legacy")
    monkeypatch.delenv("GLYPH_DATA_ROOT", raising=False)
    storage.set_data_root(target)
    assert storage.data_root() == target


def test_set_root_rejects_relative(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "pointer_path", lambda: tmp_path / "pointer.json")
    with pytest.raises(ValueError):
        storage.set_data_root("relative/path")


def test_migrate_copies_without_deleting_source(tmp_path):
    src = tmp_path / "old"
    dst = tmp_path / "new"
    (src / "sessions").mkdir(parents=True)
    (src / "sessions" / "a.jsonl").write_text("hello", encoding="utf-8")
    (src / "memory").mkdir()
    (src / "memory" / "note.md").write_text("note", encoding="utf-8")

    copied = storage.migrate_data(src, dst)

    assert copied == 2
    assert (dst / "sessions" / "a.jsonl").read_text(encoding="utf-8") == "hello"
    assert (src / "sessions" / "a.jsonl").exists()


def test_migrate_refuses_conflict(tmp_path):
    src = tmp_path / "old"
    dst = tmp_path / "new"
    (src / "sessions").mkdir(parents=True)
    (dst / "sessions").mkdir(parents=True)
    (src / "sessions" / "a.jsonl").write_text("new", encoding="utf-8")
    (dst / "sessions" / "a.jsonl").write_text("different", encoding="utf-8")

    with pytest.raises(ValueError):
        storage.migrate_data(src, dst)
