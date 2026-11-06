"""DictRepository 测试：ECDICT 行 → 前端 DictEntry。两层：

- 契约测试：临时构造与构建脚本一致的 words/forms 表，不依赖数据下载，快；
- 集成测试：若 data/dict.db 存在（default_dict_path），验证真实数据的变形反查。
"""

import sqlite3
from pathlib import Path

import pytest

from glyph.tools.dict_repo import DictRepository, default_dict_path

_SCHEMA = """
CREATE TABLE words (
  word TEXT PRIMARY KEY, phonetic TEXT, definition TEXT, translation TEXT,
  pos TEXT, collins INTEGER, oxford INTEGER, tag TEXT, bnc INTEGER,
  frq INTEGER, exchange TEXT
);
CREATE TABLE forms (form TEXT PRIMARY KEY, base TEXT);
"""


def _W(
    word: str,
    phon: str = "",
    defin: str = "",
    trans: str = "",
    tag: str = "",
    exch: str = "",
) -> tuple:
    return (word, phon, defin, trans, "", 0, 0, tag, 0, 0, exch)


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


# ---------- 契约测试（临时 db，真实 schema） ----------


def test_chinese_translation_becomes_senses(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [_W("apple", "'æpl", "n. apple fruit", "n. 苹果, 家伙\n[医] 苹果",
            tag="zk gk", exch="s:apples")],
    )
    entry = DictRepository(db).lookup("apple")
    assert entry["word"] == "apple"
    assert entry["phonetic"]["uk"] == "/'æpl/"
    assert entry["phonetic"]["us"] == "/'æpl/"
    first = entry["senses"][0]
    assert first["pos"] == "n."
    assert "苹果" in first["translation"]
    # 语域方括号标签（[医]/[法]/[网络]）不是词性：不进 pos，原样保留在译文行首
    med = entry["senses"][1]
    assert med["pos"] == ""
    assert med["translation"] == "[医] 苹果"
    assert entry["tags"] == ["中考", "高考"]
    assert entry["forms"] == ["名词复数 apples"]


def test_domain_brackets_are_not_pos(tmp_path: Path) -> None:
    # commit 真实案例：第二行 '[法] 犯, 做…' 的 [法] 是法律语域，
    # 过去被当成词性剥出，界面显示成「法：…」；[经] 同理
    db = _make_db(
        tmp_path,
        [_W("commit", "", "",
            "vt. 委托(托付), 犯罪, 指派...作战\n[法] 犯, 做, 把...交托给\n[经] 授权")],
    )
    senses = DictRepository(db).lookup("commit")["senses"]
    assert senses[0]["pos"] == "vt."
    assert [s["pos"] for s in senses[1:]] == ["", ""]
    assert senses[1]["translation"].startswith("[法]")
    assert senses[2]["translation"].startswith("[经]")


def test_case_insensitive_and_trim(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("go", "ɡəʊ", trans="v. 去")])
    assert DictRepository(db).lookup(" GO ")["word"] == "go"


def test_inflected_form_resolves_to_base(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [_W("go", "ɡəʊ", trans="v. 去", exch="p:went/d:gone/i:going")],
        forms=[("went", "go"), ("gone", "go"), ("going", "go")],
    )
    entry = DictRepository(db).lookup("went")
    assert entry is not None
    assert entry["word"] == "go"
    assert entry["queried_form"] == "went"
    assert any("过去式 went" in f for f in entry["forms"])


def test_headword_that_is_also_inflection_gets_base_form(tmp_path: Path) -> None:
    # children 本身被收录（独立词条），同时 forms 指向 child。ECDICT 对这种变形词条
    # 的 exchange 用内部代号 0（原形）/1（类型）反指，还带错拼噪声 s:childrens：
    # 应得到 base_form，且噪声 forms 被整体清掉、不展示给学习者。
    db = _make_db(
        tmp_path,
        [_W("child", trans="n. 孩子", exch="s:children"),
         _W("children", trans="pl. 孩子们", exch="0:child/1:s/s:childrens")],
        forms=[("children", "child")],
    )
    entry = DictRepository(db).lookup("children")
    assert entry["word"] == "children"
    assert entry["base_form"] == "child"
    assert "queried_form" not in entry
    # 内部代号 0/1 与错拼 childrens 都不展示：变形词条不挂常规词形
    assert "forms" not in entry


def test_parse_forms_ignores_internal_codes() -> None:
    from glyph.tools.dict_repo import _parse_forms

    # 普通词：正常变形按标签展示
    assert _parse_forms("p:went/d:gone") == ["过去式 went", "过去分词 gone"]
    # 变形词条的内部代号 0（原形）/1（类型编号）被过滤，不产生裸词噪声
    assert _parse_forms("0:child/1:s/s:childrens") == ["名词复数 childrens"]


def test_exam_tags_mapped(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("abandon", trans="v. 放弃", tag="cet4 cet6 ky gre")])
    assert DictRepository(db).lookup("abandon")["tags"] == ["CET4", "CET6", "考研", "GRE"]


def test_unknown_word_returns_none(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("go", trans="v. 去")])
    assert DictRepository(db).lookup("zzqq") is None


def test_bad_or_none_db_returns_none(tmp_path: Path) -> None:
    assert DictRepository(None).lookup("go") is None
    assert DictRepository(tmp_path / "nope.db").lookup("go") is None


def test_fallback_to_english_definition_when_no_translation(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("widget", defin="n. a small gadget", trans="")])
    assert DictRepository(db).lookup("widget")["senses"], "中文缺失时用英文兜底，senses 不空"


# ---------- 集成测试（真实 data/dict.db，缺失则 skip） ----------

_real = default_dict_path()


@pytest.mark.skipif(_real is None, reason="data/dict.db 未构建")
def test_real_irregular_forms() -> None:
    repo = DictRepository(_real)
    assert repo.lookup("went")["word"] == "go"
    children = repo.lookup("children")
    assert children["word"] == "children"
    assert children["base_form"] == "child"
    assert repo.lookup("took")["word"] == "take"


@pytest.mark.skipif(_real is None, reason="data/dict.db 未构建")
def test_real_common_word_chinese() -> None:
    entry = DictRepository(_real).lookup("reluctant")
    assert any("不情愿" in s["translation"] for s in entry["senses"])
    assert "考研" in entry["tags"]


# ---------- word_level（CEFR 近似，供 analyze_writing 用） ----------


def test_word_level_common_inflection_and_unknown(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [_W("go", trans="v. 去", tag="cet4", exch="p:went")],
        forms=[("went", "go")],
    )
    repo = DictRepository(db)
    assert repo.word_level("go") in ("A1", "A2", "B1")
    assert repo.word_level("went") in ("A1", "A2", "B1")  # 变形先反查原形
    assert repo.word_level("zzqq") is None


# ---------- 词书：按考试标签统计词量、取词表（背词功能） ----------


def _WF(word: str, tag: str, frq: int = 0) -> tuple:
    """造词书用词条：只关心 word/tag/frq（frq 为 ECDICT 词频排名，越小越高频）。"""
    row = list(_W(word, trans=f"{word} 的释义", tag=tag))
    row[9] = frq
    return tuple(row)


def test_book_stats_counts_every_tag(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [_WF("a", "zk gk", 10), _WF("b", "zk", 0), _WF("c", "cet6", 5)],
    )
    stats = {s["tag"]: s for s in DictRepository(db).book_stats()}
    assert stats["zk"]["label"] == "中考"
    assert stats["zk"]["total"] == 2
    assert stats["gk"]["total"] == 1
    assert stats["cet6"]["total"] == 1
    assert stats["gre"]["total"] == 0  # 没有词的标签也如实报 0


def test_book_stats_without_db_is_empty() -> None:
    assert DictRepository(None).book_stats() == []


def test_book_words_matches_tag_token_boundaries(tmp_path: Path) -> None:
    # 'cet4' 不能误匹配 'cet4x' 之类：按空格切 token 精确匹配
    db = _make_db(
        tmp_path,
        [_WF("real", "cet4 cet6", 1), _WF("fake", "cet4x", 2)],
    )
    page = DictRepository(db).book_words("cet4")
    assert page["words"] == ["real"]
    assert page["total"] == 1


def test_book_words_orders_by_frequency_then_alphabet(tmp_path: Path) -> None:
    # 有 frq 的高频在前（数字小=更常见）；frq 缺失（0）的排最后，内部按字母序
    db = _make_db(
        tmp_path,
        [_WF("zeta", "cet6", 0), _WF("common", "cet6", 3),
         _WF("most", "cet6", 1), _WF("mid", "cet6", 9),
         _WF("alpha", "cet6", 0)],
    )
    page = DictRepository(db).book_words("cet6")
    assert page["words"] == ["most", "common", "mid", "alpha", "zeta"]


def test_book_words_paginates_by_cursor(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_WF(f"w{i:02d}", "cet6", i + 1) for i in range(5)])
    repo = DictRepository(db)
    p1 = repo.book_words("cet6", cursor=0, limit=2)
    assert p1["words"] == ["w00", "w01"]
    assert p1["cursor"] == 0 and p1["next_cursor"] == 2 and p1["total"] == 5
    p2 = repo.book_words("cet6", cursor=2, limit=2)
    assert p2["words"] == ["w02", "w03"] and p2["next_cursor"] == 4
    p3 = repo.book_words("cet6", cursor=4, limit=2)
    assert p3["words"] == ["w04"] and p3["next_cursor"] is None


def test_book_words_rejects_unknown_tag(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DictRepository(tmp_path / "x.db").book_words("nope")


def test_book_words_without_db_is_empty_page(tmp_path: Path) -> None:
    page = DictRepository(None).book_words("cet6")
    assert page["words"] == [] and page["total"] == 0 and page["next_cursor"] is None


def test_book_words_page_carries_light_gloss_entries(tmp_path: Path) -> None:
    """词书页要带轻量释义（学习卡四选项的干扰项靠它），与 words 同序同长。"""
    db = _make_db(
        tmp_path,
        [
            _W("state", "steit", trans="n. 州, 状态\nvt. 说明, 陈述", tag="cet6"),
            _W("plain", "", trans="plain 的释义", tag="cet6"),
        ],
    )
    # frq 都为 0，按字母序：plain 在 state 前
    page = DictRepository(db).book_words("cet6")
    assert [e["word"] for e in page["entries"]] == page["words"] == ["plain", "state"]
    by_word = {e["word"]: e for e in page["entries"]}
    assert by_word["state"]["phonetic"] == "steit"
    assert by_word["state"]["pos"] == "n."
    assert by_word["state"]["gloss"] == "州, 状态"  # 只取第一义项的第一个分句
    assert by_word["plain"]["gloss"] == "plain 的释义"
    assert by_word["plain"]["pos"] in (None, "")


def test_book_words_empty_page_has_empty_entries() -> None:
    page = DictRepository(None).book_words("cet6")
    assert page["entries"] == []


@pytest.mark.skipif(_real is None, reason="data/dict.db 未构建")
def test_real_book_words_cet6() -> None:
    repo = DictRepository(_real)
    stats = {s["tag"]: s["total"] for s in repo.book_stats()}
    page = repo.book_words("cet6", limit=100)
    assert page["total"] == stats["cet6"] > 100
    assert len(page["words"]) == 100
    # 高频常见词应排在前面
    assert page["words"][0].isalpha()


# ---------- 英文释义 definitions：给词典页「英文释义」块用 ----------


def test_parse_definitions_splits_real_and_literal_newlines() -> None:
    from glyph.tools.dict_repo import _parse_definitions

    raw = "a small gadget\n\n  a thing  \ntool"
    assert _parse_definitions(raw) == ["a small gadget", "a thing", "tool"]


def test_parse_definitions_caps_at_six_lines_and_handles_empty() -> None:
    from glyph.tools.dict_repo import _parse_definitions

    raw = "\n".join(f"line {i}" for i in range(10))
    assert _parse_definitions(raw) == [f"line {i}" for i in range(6)]
    assert _parse_definitions("") == []
    assert _parse_definitions("  \n \n ") == []


def test_entry_carries_definitions_without_mixing_into_chinese_senses(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [_W("commit", trans="vt. 犯（罪）；犯下", defin="to do something wrong\n to promise")],
    )
    entry = DictRepository(db).lookup("commit")
    assert entry["definitions"] == ["to do something wrong", "to promise"]
    # 中文 senses 不被英文定义污染
    assert [s["translation"] for s in entry["senses"]] == ["犯（罪）；犯下"]


def test_entry_omits_definitions_when_definition_missing(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("commit", trans="vt. 犯（罪）", defin="")])
    entry = DictRepository(db).lookup("commit")
    assert "definitions" not in entry


def test_entry_omits_definitions_when_only_blank_lines(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("commit", trans="vt. 犯（罪）", defin=" \n \n ")])
    assert "definitions" not in DictRepository(db).lookup("commit")
