"""cefr_scale：内置 CEFR 写作 can-do 量表，以及用 ECDICT 信号近似单词等级。

seam：
- ``CEFR_WRITING_CAN_DO``：A1–C2 写作能力描述（模型对照用，公开 CEFR 量表）；
- ``estimate_word_level(tag=, frq=, oxford=)``：ECDICT 信号 → 单词 CEFR 等级近似。
"""

from glyph.tools.cefr_scale import (
    CEFR_LEVELS,
    CEFR_WRITING_CAN_DO,
    estimate_word_level,
)


def test_can_do_covers_six_levels_in_order() -> None:
    assert CEFR_LEVELS == ("A1", "A2", "B1", "B2", "C1", "C2")
    assert list(CEFR_WRITING_CAN_DO) == list(CEFR_LEVELS)
    assert all(CEFR_WRITING_CAN_DO[lvl] for lvl in CEFR_LEVELS)


def test_high_frequency_word_is_low_level() -> None:
    assert estimate_word_level(tag=None, frq=500, oxford=1) in ("A1", "A2")


def test_gre_tag_is_high_level() -> None:
    assert estimate_word_level(tag="gre", frq=20000, oxford=0) in ("C1", "C2")


def test_cet4_is_b1_band() -> None:
    assert estimate_word_level(tag="cet4", frq=2000, oxford=0) in ("A2", "B1")


def test_conflicting_signals_take_the_lower() -> None:
    # 高频常见词即使被打了高难 tag，也应从低判，避免把常见词误判为难词
    assert estimate_word_level(tag="gre", frq=300, oxford=1) in ("A1", "A2", "B1")


def test_no_signal_returns_a_defined_level() -> None:
    lvl = estimate_word_level(tag=None, frq=0, oxford=0)
    assert lvl in CEFR_LEVELS
