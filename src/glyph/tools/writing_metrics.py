"""writing_metrics：作文硬指标的确定性计算（纯函数、不触网、不依赖 LLM）。

这是作文等级分析的"证据层"：句数 / 句长分布、词数、音节、复杂结构（被动、从句、
衔接词）、Flesch 可读性，全部由文本直接算出、可复算。analyze_writing 工具在此
基础上再让模型对照 CEFR 量表——模型只能"对照证据下判断"，不能跳过证据凭空给等级。

所有结构计数都是**近似模式匹配**（自然语言不可能用正则精确解析），仅用于等级对照，
不作为语法裁判；这一点会在工具输出的 warnings 里说明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["WritingMetrics", "analyze_text", "count_syllables"]

# be + 过去分词（ed / en / wn）；以及 "动词ed by"
_PASSIVE_BE = r"\b(?:am|is|are|was|were|been|being|be)\s+[a-z]+(?:ed|en|wn)\b"
_PASSIVE_BY = r"\b[a-z]+ed\s+by\b"
# 从属/从句引导词（that 多义、噪声大，第一版不计）
_SUBORDINATE = (
    r"\b(?:because|although|though|unless|whereas|while|when|if|since|"
    r"which|who|whom|whose|where|whenever|wherever)\b"
)
# 句间逻辑衔接词
_LINKER = (
    r"\b(?:however|therefore|moreover|furthermore|nevertheless|nonetheless|thus|hence|"
    r"consequently|meanwhile|otherwise|besides|instead|accordingly|additionally|"
    r"likewise|similarly|firstly|secondly|thirdly|finally)\b"
)
_LINKER_PHRASES = (
    r"in addition",
    r"for example",
    r"for instance",
    r"in conclusion",
    r"on the other hand",
    r"as a result",
    r"in contrast",
    r"first of all",
)

_WORD = r"[A-Za-z]+(?:'[A-Za-z]+)?"


@dataclass(frozen=True)
class WritingMetrics:
    sentence_count: int
    word_count: int
    avg_sentence_length: float
    short_sentences: int  # <8 词
    medium_sentences: int  # 8–20 词
    long_sentences: int  # >20 词
    syllables: int
    passive_voice: int
    subordinate_clauses: int
    linkers: int
    flesch_reading_ease: float
    fk_grade: float


def count_syllables(word: str) -> int:
    """近似英语音节数：数元音组，扣除词尾静默 e（-le 构成的音节不扣）。"""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    count = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and not w.endswith(("le", "ee", "ie", "ye")) and len(w) > 2:
        count -= 1  # 词尾静默 e
    return max(count, 1)


def analyze_text(text: str) -> WritingMetrics:
    text = text or ""
    low = text.lower()

    # 句子：按句末标点切，只保留含字母/数字的片段（纯标点、空白不计句）
    sentences = [s for s in re.split(r"[.!?]+", text) if re.search(r"[A-Za-z0-9]", s)]
    sentence_count = len(sentences)

    words = re.findall(_WORD, text)
    word_count = len(words)

    short = medium = long = 0
    for s in sentences:
        n = len(re.findall(_WORD, s))
        if n < 8:
            short += 1
        elif n <= 20:
            medium += 1
        else:
            long += 1

    syllables = sum(count_syllables(w) for w in words)
    passive = len(re.findall(_PASSIVE_BE, low)) + len(re.findall(_PASSIVE_BY, low))
    subordinate = len(re.findall(_SUBORDINATE, low))
    linkers = len(re.findall(_LINKER, low)) + sum(
        len(re.findall(p, low)) for p in _LINKER_PHRASES
    )

    if sentence_count and word_count:
        words_per_sentence = word_count / sentence_count
        syllables_per_word = syllables / word_count
        flesch = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
        fk_grade = 0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59
    else:
        flesch = 0.0
        fk_grade = 0.0

    return WritingMetrics(
        sentence_count=sentence_count,
        word_count=word_count,
        avg_sentence_length=(word_count / sentence_count if sentence_count else 0.0),
        short_sentences=short,
        medium_sentences=medium,
        long_sentences=long,
        syllables=syllables,
        passive_voice=passive,
        subordinate_clauses=subordinate,
        linkers=linkers,
        flesch_reading_ease=flesch,
        fk_grade=fk_grade,
    )
