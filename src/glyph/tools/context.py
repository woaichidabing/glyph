"""句子边界单点 + 复核用的局部上下文切分（纯函数，离线可测）。

``SENTENCE_END`` 从 ``spell.py`` 上提到这里，是**全项目句子边界的单点**（R4）：
拼写一处的句末判定与复核一处的切句必须是同一套，否则「拼写认为是一句、
复核切成两句」这类错位没人会发现。

``locate_context()`` 按设计文档 §1.8 的算法 L0–L7 实现，切出给模型复核用的
**局部**上下文——整句为主，句子太短或定位不到才升段落。
"""

from __future__ import annotations

import re
from typing import Literal

from ..runtime.errors import ToolError

# 复核时给模型看的上下文范围：整句为主，句子太短或定位不到才扩到段落。
# 定义在这里（而非 grading_models）是依赖方向的约定：grading_models → context 单向。
ContextScope = Literal["sentence", "paragraph"]

# 句子结束符。\n 也算：txt 里的软折行常见，且 spell.py 原本就这么判定
SENTENCE_END = frozenset(".!?\n")
# 句末标点后面可能紧跟的收尾符号（引号、括号…），要跟着一起吞掉
_CLOSERS = frozenset("\"'”’)]}")
# 这些缩写里的句点不是句末（小写形式，判定时统一 lower）
_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "fig",
        "no",
        "etc",
        "e.g",
        "i.e",
        "a.m",
        "p.m",
        "u.s",
        "u.k",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
    }
)
# 一个「带句点的词」：Mr. / e.g. / U.S. / J. —— 用来一次性认下整串句点
_DOTTED_WORD_RE = re.compile(r"[A-Za-z]+(?:\.[A-Za-z]+)*\.")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")

__all__ = ["ContextScope", "SENTENCE_END", "locate_context"]


def _abbrev_dots(text: str) -> frozenset[int]:
    """标出 ``text`` 里所有**属于缩写**的句点下标。

    为什么按「整串」认而不是逐个判：``e.g.`` 有两个句点，若只看「句点前一个单词」，
    第二个句点认得出来（``e.g`` 在表里），第一个却会被当成句末——句子被拦腰切断。
    """
    dots: set[int] = set()
    for match in _DOTTED_WORD_RE.finditer(text):
        token = match.group()[:-1]  # 去掉末尾那个句点
        known = token.lower() in _ABBREVIATIONS
        initial = len(token) == 1 and token.isupper()  # 首字母缩写：John J. Smith
        if known or initial:
            dots.update(i for i in range(match.start(), match.end()) if text[i] == ".")
    return frozenset(dots)


def _left_bound(text: str, anchor: int, abbrev: frozenset[int]) -> int:
    """向左扫句子左界：停在「句末标点 + 收尾符号 + 空白」的右侧。"""
    i = anchor - 1
    while i >= 0:
        if text[i] in SENTENCE_END and i not in abbrev:
            j = i + 1
            while j < len(text) and text[j] in _CLOSERS:
                j += 1
            while j < len(text) and text[j].isspace():
                j += 1
            return j
        i -= 1
    return 0


def _right_bound(text: str, anchor: int, abbrev: frozenset[int]) -> int:
    """向右扫句子右界：停在句末标点（连带收尾符号与连续结束符）之后。"""
    i = anchor
    while i < len(text):
        if text[i] in SENTENCE_END and i not in abbrev:
            j = i + 1
            while j < len(text) and text[j] in _CLOSERS:
                j += 1
            while j < len(text) and text[j] in SENTENCE_END:
                j += 1
            return j
        i += 1
    return len(text)


def _paragraph_bounds(text: str, pos: int) -> tuple[int, int]:
    """取 ``pos`` 所在段落的边界（按空行分段；没有空行则整篇算一段）。"""
    bounds: list[tuple[int, int]] = []
    start = 0
    for match in _PARA_SPLIT_RE.finditer(text):
        bounds.append((start, match.start()))
        start = match.end()
    bounds.append((start, len(text)))

    for seg_start, seg_end in bounds:
        if seg_start <= pos < seg_end:
            return seg_start, seg_end
    return bounds[-1] if bounds else (0, len(text))


def _truncate_at_sentence_end(segment: str, max_chars: int) -> str:
    """超硬顶时按最近的句尾向左截断，尽量不留下半句话。"""
    for idx in range(min(max_chars, len(segment)) - 1, -1, -1):
        if segment[idx] in SENTENCE_END:
            return segment[: idx + 1]
    return segment[:max_chars]


def locate_context(
    text: str,
    start: int | None,
    end: int | None,
    *,
    original: str = "",
    min_words: int = 8,
    max_chars: int = 4096,
    warnings: list[str] | None = None,
) -> tuple[str, ContextScope]:
    """切出复核用的局部上下文，返回 ``(切片, 范围)``。

    算法 L0–L7（设计文档 §1.8）：

    - L0 文本为空 → ``ToolError``；
    - L1 先用 ``start/end`` 定锚点，定位不到就用 ``original`` 在文本里找，
      还找不到 → ``ToolError``（使用者把那段话改掉了，不能拿全文去复核）；
    - L2 越界夹回；L3 切句（带缩写保护）；L4 得到整句；
    - L5 ``start`` 为 None（当初就没定位到）或句子不足 ``min_words`` 词 → 升段落；
    - L6 超过 ``max_chars`` → 按最近句尾左截，并往 ``warnings`` 记一条；
    - L7 切片是原文原样子串，**不 trim 内部空白**——任何加工都会让模型看到的
      文本与使用者原文产生偏差。

    ``warnings`` 是可选的出参容器（规格签名里没有，L6 又要求「记一条」）：签名本身
    只回 ``(切片, 范围)``，需要提示的调用方传入一个 list 即可，不传就静默。
    """
    # L0
    if not text or not text.strip():
        raise ToolError("待复核的文本为空，无法复核")

    # L1 定锚点
    if start is not None and end is not None:
        anchor_start, anchor_end, positioned = start, end, True
    elif original:
        pos = text.find(original)
        if pos < 0:
            raise ToolError("这条建议在当前文本里找不到原文片段，无法复核")
        anchor_start, anchor_end, positioned = pos, pos + len(original), False
    else:
        raise ToolError("这条建议在当前文本里找不到原文片段，无法复核")

    # L2 clamp（使用者改过文本导致越界是正常的，夹回来即可）
    anchor_start = max(0, min(anchor_start, len(text)))
    anchor_end = max(anchor_start, min(anchor_end, len(text)))

    # L5a 当初就没定位到 → 直接给段落
    if not positioned:
        seg_start, seg_end = _paragraph_bounds(text, anchor_start)
        segment: str = text[seg_start:seg_end]
        scope: ContextScope = "paragraph"
    else:
        # L3/L4 整句
        abbrev = _abbrev_dots(text)
        left = _left_bound(text, anchor_start, abbrev)
        right = _right_bound(text, anchor_end, abbrev)
        segment = text[left:right]

        # L5b 句子太短 → 升段落（短句里判断不了指代、跨句时态）
        if len(segment.split()) < min_words:
            seg_start, seg_end = _paragraph_bounds(text, anchor_start)
            segment = text[seg_start:seg_end]
            scope = "paragraph"
        else:
            scope = "sentence"

    # L6 硬顶（BC-15 明确要求段落级也受它保护）
    if len(segment) > max_chars:
        segment = _truncate_at_sentence_end(segment, max_chars)
        if warnings is not None:
            warnings.append(
                f"上下文超过 {max_chars} 字符，已按最近的句尾截断，复核只看到了部分上下文"
            )

    # L7 原样子串，不 trim
    return segment, scope
