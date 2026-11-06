"""本地拼写检查器测试（离线，pyspellchecker 自带词典，不联网、0 token）。

重点不是"能抓错"，而是降噪：缩写、专有名词/拼音、数字不许误报；
只对真正的拼写词下手，且字符区间必须精确（供原位高亮）。
"""

import pytest

from glyph.tools.spell import PySpellingChecker


@pytest.fixture
def checker() -> PySpellingChecker:
    return PySpellingChecker()


async def test_spell_catches_lowercase_misspelling_with_exact_span(checker):
    essay = "I am a studdent."

    [d] = await checker.check(essay)

    assert d.original == "studdent"
    assert d.corrected == "student"
    assert d.source == "spell" and d.type == "hard" and d.tier == "error"
    assert essay[d.start : d.end] == "studdent"
    assert d.explanation_zh


async def test_spell_clean_text_has_no_false_positive(checker):
    essay = "I am a happy student. She goes to school every day."

    assert await checker.check(essay) == []


async def test_spell_skips_all_caps_abbreviation(checker):
    essay = "The USA is big."

    assert await checker.check(essay) == []


async def test_spell_skips_capitalized_proper_name_mid_sentence(checker):
    # Xiaoming 是拼音人名：拼写库不认识，但首字母大写且不在句首，按专名放过
    essay = "Yesterday I met Xiaoming."

    assert await checker.check(essay) == []


async def test_spell_checks_capitalized_word_at_sentence_start(checker):
    essay = "Teh cat sat."

    [d] = await checker.check(essay)

    assert d.original == "Teh"
    assert d.corrected == "The"  # 纠错后还原句首大写
    assert essay[d.start : d.end] == "Teh"


async def test_spell_ignores_numbers(checker):
    essay = "I have 2 apples and 3 oranges."

    assert await checker.check(essay) == []


async def test_spell_multiple_findings_have_exact_spans_in_order(checker):
    essay = "I recieve a messge."

    dets = await checker.check(essay)

    assert [d.original for d in dets] == ["recieve", "messge"]
    assert [d.corrected for d in dets] == ["receive", "message"]
    for d in dets:
        assert essay[d.start : d.end] == d.original


async def test_spell_finding_is_tagged_spell_code(checker):
    # 本地拼写命中 100% 是拼写问题，代码直接盖 SPELL（D19），不问模型
    [d] = await checker.check("I am a studdent.")

    assert d.type == "hard" and d.tier == "error"
    assert d.error_code == "SPELL"
