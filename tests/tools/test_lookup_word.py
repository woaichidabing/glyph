"""lookup_word agent 工具测试：包装 DictRepository，给模型返回结构化查词结果。

seam = ``LookupWordTool(repo).run({"word": ...})``；repo 指向临时 words/forms db
（schema 与 scripts/build_dict_db.py 一致），不依赖数据下载。
"""

import sqlite3
from pathlib import Path

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.dict_repo import DictRepository
from glyph.tools.lookup_word import LookupWordTool

_SCHEMA = """
CREATE TABLE words (
  word TEXT PRIMARY KEY, phonetic TEXT, definition TEXT, translation TEXT,
  pos TEXT, collins INTEGER, oxford INTEGER, tag TEXT, bnc INTEGER,
  frq INTEGER, exchange TEXT
);
CREATE TABLE forms (form TEXT PRIMARY KEY, base TEXT);
"""


def _make_db(tmp_path: Path, words: list[tuple], forms: list[tuple] | None = None) -> Path:
    db = tmp_path / "dict.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO words (word,phonetic,definition,translation,pos,collins,oxford,"
        "tag,bnc,frq,exchange) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        words,
    )
    if forms:
        con.executemany("INSERT INTO forms (form,base) VALUES (?,?)", forms)
    con.commit()
    con.close()
    return db


def _W(word: str, trans: str = "", tag: str = "", frq: int = 0, exch: str = "") -> tuple:
    return (word, "", "", trans, "", 0, 0, tag, 0, frq, exch)


async def test_lookup_word_found(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [_W("go", "v. 去；走", tag="cet4", frq=500, exch="p:went/d:gone")],
        forms=[("went", "go"), ("gone", "go")],
    )
    out = await LookupWordTool(DictRepository(db)).run({"word": "go"})

    assert out["found"] is True
    assert out["word"] == "go"
    assert any("去" in s["translation"] for s in out["senses"])
    assert any("went" in f for f in out["forms"])


async def test_lookup_inflected_word(tmp_path: Path) -> None:
    # 用户/模型查变形 went，工具应反查回 go
    db = _make_db(tmp_path, [_W("go", "v. 去", exch="p:went")], forms=[("went", "go")])
    out = await LookupWordTool(DictRepository(db)).run({"word": "went"})
    assert out["found"] is True
    assert out["word"] == "go"
    assert out["queried_form"] == "went"


async def test_lookup_word_not_found_is_data_not_error(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("go", "v. 去")])
    out = await LookupWordTool(DictRepository(db)).run({"word": "qqzz"})

    assert out["found"] is False
    assert out["word"] == "qqzz"
    assert out.get("note")


async def test_lookup_word_empty_word_raises(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("go", "v. 去")])
    with pytest.raises(ToolError):
        await LookupWordTool(DictRepository(db)).run({"word": "   "})
