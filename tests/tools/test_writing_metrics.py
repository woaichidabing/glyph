"""writing_metrics：作文硬指标的确定性计算（纯函数、不触网、不依赖 LLM）。

seam = ``analyze_text(text)``：给定英文文本，算句数/句长分布、词数、复杂结构
（被动、从句、衔接词）、音节与 Flesch 可读性。这些是"证据"，analyze_writing
工具在此基础上再让模型对照 CEFR 量表，模型不能跳过证据凭空给等级。
"""

import pytest

from glyph.tools.writing_metrics import analyze_text, count_syllables


def test_counts_sentences_and_words() -> None:
    m = analyze_text("I like apples. They are sweet and red. Do you like them too?")
    assert m.sentence_count == 3
    assert m.word_count == 13
    assert m.avg_sentence_length == pytest.approx(13 / 3)


def test_short_long_distribution() -> None:
    m = analyze_text("I run. He jumps. We sit.")
    assert m.short_sentences == 3
    assert m.long_sentences == 0


def test_long_sentence_detected() -> None:
    text = (
        "The old library that stands at the corner of the quiet street has been "
        "keeping thousands of dusty books for many generations of local readers."
    )
    m = analyze_text(text)
    assert m.sentence_count == 1
    assert m.long_sentences == 1  # >20 词


def test_syllable_count() -> None:
    assert count_syllables("cat") == 1
    assert count_syllables("apple") == 2
    assert count_syllables("beautiful") == 3
    assert count_syllables("a") == 1  # 至少 1


def test_subordinate_clauses() -> None:
    m = analyze_text("I stayed home because it rained, although I had planned to go out.")
    assert m.subordinate_clauses >= 2  # because, although


def test_passive_voice() -> None:
    m = analyze_text("The cake was eaten by the children.")
    assert m.passive_voice >= 1


def test_linkers() -> None:
    m = analyze_text("It was raining. However, we went out. Furthermore, we enjoyed the walk.")
    assert m.linkers >= 2  # however, furthermore


def test_flesch_simple_text_is_high() -> None:
    m = analyze_text("The cat sat on the mat. It was a good cat.")
    assert isinstance(m.flesch_reading_ease, float)
    assert m.flesch_reading_ease > 60  # 简单文本分数高


def test_empty_and_whitespace() -> None:
    m = analyze_text("   ... ??? ")
    assert m.sentence_count == 0
    assert m.word_count == 0
    assert m.avg_sentence_length == 0
