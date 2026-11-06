"""生词本 / 查词历史本地 store 的离线测试（TDD 先写）。

覆盖：空状态、add 幂等、remove/restore、lookup 计数与裁剪、import 合并、
坏 JSON、原子替换与并发写、词形校验、clock 注入。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from glyph.wordbook import WordbookStore
from glyph.wordbook.paths import GLYPH_WORDBOOK_ROOT_ENV, state_path, wordbook_root
from glyph.wordbook.store import MAX_HISTORY, MAX_ITEMS


class Clock:
    """可拨动的毫秒时钟；store 的 clock 参数注入用。"""

    def __init__(self, start: int = 1_760_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def tick(self, ms: int = 1000) -> int:
        self.now += ms
        return self.now


def _entry(
    word: str = "commit",
    *,
    translation: str = "犯（罪）；犯下",
    phonetic: dict | None = None,
) -> dict:
    return {
        "word": word,
        "phonetic": phonetic if phonetic is not None else {"uk": "/kəˈmɪt/", "us": "/kəˈmɪt/"},
        "senses": [{"pos": "v.", "translation": translation, "examples": []}],
        "source": "api",
    }


def test_empty_snapshot_does_not_create_file(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    assert store.snapshot() == {"items": [], "history": []}
    assert not state_path(tmp_path).exists()


def test_add_snapshots_entry_and_persists_state_file(tmp_path: Path) -> None:
    clock = Clock()
    store = WordbookStore(tmp_path, clock=clock)
    snapshot, item = store.add(_entry())

    assert item == {
        "word": "commit",
        "addedAt": clock.now,
        "phonetic": {"uk": "/kəˈmɪt/", "us": "/kəˈmɪt/"},
        "gloss": "犯（罪）；犯下",
    }
    assert snapshot == {"items": [item], "history": []}

    raw = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["items"] == [item]
    assert raw["history"] == []


def test_snapshot_is_deep_copy(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    snapshot, item = store.add(_entry())
    snapshot["items"][0]["gloss"] = "被改了"
    item["gloss"] = "也被改了"
    assert store.snapshot()["items"][0]["gloss"] == "犯（罪）；犯下"


def test_add_is_idempotent_by_casefold_and_keeps_original(tmp_path: Path) -> None:
    clock = Clock(1000)
    store = WordbookStore(tmp_path, clock=clock)
    store.add(_entry("commit"), added_at=1000)

    clock.tick()
    snapshot, item = store.add(_entry("  COMMIT  ", translation="别的释义"), added_at=2000)

    assert len(snapshot["items"]) == 1
    assert item["word"] == "commit"
    assert item["addedAt"] == 1000
    assert item["gloss"] == "犯（罪）；犯下"


def test_add_without_senses_uses_empty_gloss_and_optional_phonetic(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path, clock=lambda: 123)
    snapshot, item = store.add({"word": "plain", "senses": []})
    assert item["gloss"] == ""
    assert "phonetic" not in item
    assert snapshot["items"] == [item]


def test_add_caps_items_at_max_and_newest_first(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    for i in range(MAX_ITEMS + 5):
        store.add({"word": f"w{i}", "senses": []}, added_at=i)
    snapshot = store.snapshot()
    assert len(snapshot["items"]) == MAX_ITEMS
    assert snapshot["items"][0]["word"] == f"w{MAX_ITEMS + 4}"
    assert snapshot["items"][-1]["word"] == "w5"


def test_remove_and_restore_keeps_original_position(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path, clock=lambda: 9999)
    store.add({"word": "older", "senses": []}, added_at=1000)
    store.add({"word": "newer", "senses": []}, added_at=2000)

    snapshot, removed = store.remove("OLDER")
    assert removed is not None and removed["word"] == "older"
    assert [i["word"] for i in snapshot["items"]] == ["newer"]

    snapshot, restored = store.restore(removed)
    assert [i["word"] for i in snapshot["items"]] == ["newer", "older"]
    assert restored["addedAt"] == 1000

    # 已存在：保留原记录，不产生重复
    snapshot, again = store.restore({"word": "newer", "addedAt": 1, "gloss": "x"})
    assert len(snapshot["items"]) == 2
    assert again["addedAt"] == 2000


def test_remove_missing_returns_none_without_error(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    snapshot, removed = store.remove("nothing")
    assert removed is None
    assert snapshot == {"items": [], "history": []}


def test_restore_missing_added_at_uses_clock(tmp_path: Path) -> None:
    clock = Clock(4242)
    store = WordbookStore(tmp_path, clock=clock)
    _, item = store.restore({"word": "commit", "gloss": "犯"})
    assert item["addedAt"] == 4242


def test_record_lookup_counts_updates_and_moves_to_front(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path, clock=lambda: 9999)
    snapshot, item = store.record_lookup("commit", at=100)
    assert item == {"word": "commit", "at": 100, "count": 1}
    assert snapshot["history"] == [item]

    snapshot, item = store.record_lookup(" COMMIT ", at=200)
    assert len(snapshot["history"]) == 1
    assert item["count"] == 2 and item["at"] == 200

    snapshot, _ = store.record_lookup("other", at=150)
    assert [h["word"] for h in snapshot["history"]] == ["other", "commit"]


def test_record_lookup_caps_history_at_max(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    for i in range(MAX_HISTORY + 3):
        store.record_lookup(f"h{i}", at=i)
    history = store.snapshot()["history"]
    assert len(history) == MAX_HISTORY
    assert history[0]["word"] == f"h{MAX_HISTORY + 2}"
    assert history[-1]["word"] == "h3"


def test_clear_items_keeps_history_and_vice_versa(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    store.add({"word": "commit", "senses": []}, added_at=1)
    store.record_lookup("commit", at=1)

    snapshot = store.clear_items()
    assert snapshot["items"] == []
    assert [h["word"] for h in snapshot["history"]] == ["commit"]

    store.add({"word": "commit", "senses": []}, added_at=2)
    snapshot = store.clear_history()
    assert snapshot["history"] == []
    assert [i["word"] for i in snapshot["items"]] == ["commit"]


def test_import_merges_and_is_idempotent(tmp_path: Path) -> None:
    clock = Clock(3000)
    store = WordbookStore(tmp_path, clock=clock)
    store.add(_entry("commit"), added_at=1000)
    store.record_lookup("commit", at=100)

    imported_items = [
        {"word": "COMMIT", "addedAt": 9999, "gloss": "不该覆盖"},
        {"word": "restore", "addedAt": 2000, "phonetic": {"uk": "", "us": ""}, "gloss": "恢复"},
        {"word": "without-time", "gloss": "没时间戳"},
    ]
    imported_history = [
        {"word": "commit", "at": 2000, "count": 5},
        {"word": "new-word", "at": 500, "count": 1},
    ]

    snapshot, n_items, n_history = store.import_state(imported_items, imported_history)
    assert n_items == 2  # commit 已存在，只有 restore / without-time 是新增
    assert n_history == 1  # commit 合并、new-word 新增
    by_word = {i["word"].casefold(): i for i in snapshot["items"]}
    assert by_word["commit"]["addedAt"] == 1000
    assert by_word["commit"]["gloss"] == "犯（罪）；犯下"
    assert by_word["without-time"]["addedAt"] == 3000
    history = {h["word"].casefold(): h for h in snapshot["history"]}
    assert history["commit"]["count"] == 5 and history["commit"]["at"] == 2000
    assert history["new-word"]["count"] == 1

    first = store.snapshot()
    snapshot, n_items, n_history = store.import_state(imported_items, imported_history)
    assert (n_items, n_history) == (0, 0)
    assert snapshot == first


def test_import_rejects_bad_word_without_writing(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    store.add({"word": "keep", "senses": []}, added_at=1)
    before = store.snapshot()
    with pytest.raises(ValueError):
        store.import_state([{"word": "ok"}, {"word": "bad\nword"}], [])
    assert store.snapshot() == before


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65, "two\nlines", "two\rlines", None, 123])
def test_invalid_word_raises_value_error(tmp_path: Path, bad: object) -> None:
    store = WordbookStore(tmp_path)
    with pytest.raises(ValueError):
        store.add({"word": bad, "senses": []})
    with pytest.raises(ValueError):
        store.remove(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.record_lookup(bad)  # type: ignore[arg-type]


def test_bad_json_is_treated_as_empty_and_next_write_repairs(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    store = WordbookStore(tmp_path)
    assert store.snapshot() == {"items": [], "history": []}

    snapshot, item = store.add({"word": "commit", "senses": []}, added_at=1)
    assert snapshot["items"] == [item]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["items"][0]["word"] == "commit"


def test_structurally_bad_state_is_treated_as_empty(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": "nope", "history": {"x": 1}}), encoding="utf-8")
    assert WordbookStore(tmp_path).snapshot() == {"items": [], "history": []}


def test_state_is_shared_across_store_instances(tmp_path: Path) -> None:
    WordbookStore(tmp_path).add({"word": "commit", "senses": []}, added_at=1)
    again = WordbookStore(tmp_path).snapshot()
    assert again["items"][0]["word"] == "commit"


def test_atomic_replace_is_used_and_temp_files_are_cleaned(tmp_path: Path, monkeypatch) -> None:
    import glyph.wordbook.store as store_mod

    real_replace = store_mod.os.replace
    calls: list[tuple[str, str]] = []

    def spy(src, dst):
        calls.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", spy)
    WordbookStore(tmp_path).add({"word": "commit", "senses": []}, added_at=1)

    assert calls and Path(calls[0][1]) == state_path(tmp_path)
    assert Path(calls[0][0]).name.startswith(".state.")
    assert not list(tmp_path.glob(".state.*"))


def test_concurrent_adds_do_not_lose_writes(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)

    def worker(i: int) -> None:
        store.add({"word": f"w{i}", "senses": []}, added_at=i)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(60)))

    snapshot = store.snapshot()
    assert len(snapshot["items"]) == 60
    assert {i["word"] for i in snapshot["items"]} == {f"w{i}" for i in range(60)}
    raw = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert len(raw["items"]) == 60
    assert not list(tmp_path.glob(".state.*"))


def test_root_env_override(tmp_path: Path, monkeypatch) -> None:
    custom = tmp_path / "custom-root"
    monkeypatch.setenv(GLYPH_WORDBOOK_ROOT_ENV, str(custom))
    assert wordbook_root() == custom
    WordbookStore().add({"word": "commit", "senses": []}, added_at=1)
    assert (custom / "state.json").exists()
