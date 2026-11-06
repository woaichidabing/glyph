"""propose_wordbook_add：给对话里的“收词确认卡”递数据，只读、不写。

契约：
- 只提议、不收藏；真正写入由用户在卡片上点“收进生词本”后走现有 wordbook 接口。
- 本地词库查不到 / 没有可用释义 / 已经在生词本里 → 不递卡，回一句给模型的话。
- 查词历史次数随结果一起给，卡片才能写“你查过 N 次”。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from glyph.tools.dict_repo import DictRepository
from glyph.tools.wordbook_proposal import ProposeWordbookAddTool
from glyph.wordbook import WordbookStore

_SCHEMA = """
CREATE TABLE words (
  word TEXT PRIMARY KEY, phonetic TEXT, definition TEXT, translation TEXT,
  pos TEXT, collins INTEGER, oxford INTEGER, tag TEXT, bnc INTEGER,
  frq INTEGER, exchange TEXT
);
CREATE TABLE forms (form TEXT PRIMARY KEY, base TEXT);
"""


def _make_db(tmp_path: Path, words: list[tuple]) -> Path:
    db = tmp_path / "dict.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO words (word,phonetic,definition,translation,pos,collins,oxford,"
        "tag,bnc,frq,exchange) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        words,
    )
    con.commit()
    con.close()
    return db


def _word(
    word: str,
    *,
    phonetic: str = "",
    definition: str = "",
    translation: str = "",
) -> tuple:
    return (word, phonetic, definition, translation, "", 0, 0, "", 0, 0, "")


def _tool(tmp_path: Path, store: WordbookStore | None = None) -> ProposeWordbookAddTool:
    db = _make_db(
        tmp_path,
        [
            _word(
                "commit",
                phonetic="kəˈmɪt",
                translation="v. 承诺；投入；犯（错）",
            ),
            _word("plain", definition="to promise"),
        ],
    )
    return ProposeWordbookAddTool(DictRepository(db), store)


async def test_proposes_known_word_with_phonetic_and_gloss(tmp_path: Path) -> None:
    out = await _tool(tmp_path).run({"word": "commit"})

    assert out["status"] == "proposed"
    assert out["word"] == "commit"
    assert out["phonetic"] == {"uk": "/kəˈmɪt/", "us": "/kəˈmɪt/"}
    assert out["pos"] == "v."
    assert out["gloss"] == "承诺；投入；犯（错）"
    assert out["lookedUpCount"] == 0
    assert "不要" in out["summary"]


async def test_counts_history_for_the_word(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    store.record_lookup("commit")
    store.record_lookup("Commit")  # 大小写同词

    out = await _tool(tmp_path, store).run({"word": "commit"})

    assert out["status"] == "proposed"
    assert out["lookedUpCount"] == 2


async def test_already_collected_does_not_propose(tmp_path: Path) -> None:
    store = WordbookStore(tmp_path)
    store.add({"word": "commit", "senses": [{"translation": "承诺"}]})

    out = await _tool(tmp_path, store).run({"word": "Commit"})

    assert out["status"] == "already_collected"
    assert out["word"] == "commit"
    assert "已经在生词本" in out["summary"]


async def test_unknown_word_is_data_not_a_card(tmp_path: Path) -> None:
    out = await _tool(tmp_path).run({"word": "qqzz"})

    assert out["status"] == "not_found"
    assert out["word"] == "qqzz"
    assert out["summary"]


async def test_empty_word_is_tool_error(tmp_path: Path) -> None:
    from glyph.runtime.errors import ToolError

    with pytest.raises(ToolError):
        await _tool(tmp_path).run({"word": "   "})


async def test_too_long_word_is_tool_error(tmp_path: Path) -> None:
    from glyph.runtime.errors import ToolError

    with pytest.raises(ToolError) as exc:
        await _tool(tmp_path).run({"word": "a" * 65})

    assert "64" in str(exc.value)


async def test_english_definition_falls_back_to_gloss(tmp_path: Path) -> None:
    out = await _tool(tmp_path).run({"word": "plain"})

    assert out["status"] == "proposed"
    assert out["gloss"] == "to promise"


async def test_word_without_any_gloss_is_not_proposed(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_word("empty")])
    tool = ProposeWordbookAddTool(DictRepository(db))

    out = await tool.run({"word": "empty"})

    assert out["status"] == "no_gloss"
    assert out["summary"]


async def test_store_failure_degrades_to_zero_count(tmp_path: Path) -> None:
    class _BrokenStore(WordbookStore):
        def snapshot(self):
            raise OSError("disk gone")

    out = await _tool(tmp_path, _BrokenStore(tmp_path)).run({"word": "commit"})

    assert out["status"] == "proposed"
    assert out["lookedUpCount"] == 0
