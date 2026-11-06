"""本地拼写检查（离线、0 token）。

只做拼写库擅长的硬错误：单词拼错。时态、三单、冠词、中式英语等交给批改
模型。降噪优先（拼写库不认识专有名词/拼音，误报比漏报更伤人）：

- 全大写缩写（USA、CEO）跳过；
- 句中首字母大写的词按专有名词/拼音跳过（只检查句首大写词）；
- 单字母（I、a）与含撇号缩写（don't、it's）跳过，交给模型；
- 数字天然不进入单词正则。
"""

from __future__ import annotations

import re
from typing import Protocol

from .context import SENTENCE_END  # 句子边界全项目单点（R4），与复核切句同源
from .grading_models import Detection

# 单词：字母序列，允许一个撇号屈折（don't 本身会被白名单跳过，这里仅保证切分）
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


class SpellingChecker(Protocol):
    """拼写检查器协议：输入原文，输出拼写类 Detection（已带字符区间）。"""

    async def check(self, text: str) -> list[Detection]: ...


def _is_sentence_start(text: str, start: int) -> bool:
    """该单词之前（跳过空白）是否为句首：文本开头或句末标点/换行之后。"""
    j = start - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    return j < 0 or text[j] in SENTENCE_END


class PySpellingChecker:
    """基于 pyspellchecker 自带英文词典的本地拼写检查器。"""

    def __init__(self) -> None:
        self._spell = None

    def _ensure(self):
        if self._spell is None:
            try:
                from spellchecker import SpellChecker as _PySpell
            except ImportError as exc:  # pragma: no cover - 依赖缺失分支
                raise RuntimeError(
                    "本地拼写检查需要 pyspellchecker，请安装 markr 的 grade 额外依赖"
                ) from exc
            self._spell = _PySpell()
        return self._spell

    async def check(self, text: str) -> list[Detection]:
        return self._check_sync(text)

    def _check_sync(self, text: str) -> list[Detection]:
        spell = self._ensure()
        to_check: list[tuple[re.Match[str], str, str, bool]] = []

        for match in _WORD_RE.finditer(text):
            word = match.group(0)
            if len(word) <= 1:
                continue  # I、a 等单字母是合法词
            if "'" in word:
                continue  # don't、it's 等屈折/缩写交给模型，避免误报
            if word.isupper():
                continue  # USA、CEO 等缩写
            capitalized = word[0].isupper()
            if capitalized and not _is_sentence_start(text, match.start()):
                continue  # 句中首字母大写：按专有名词/拼音放过
            lookup = word.lower() if capitalized else word
            to_check.append((match, word, lookup, capitalized))

        if not to_check:
            return []

        unknown = spell.unknown([item[2] for item in to_check])

        detections: list[Detection] = []
        for match, word, lookup, capitalized in to_check:
            if lookup not in unknown:
                continue
            corrected = spell.correction(lookup) or ""
            if capitalized and corrected:
                corrected = corrected[0].upper() + corrected[1:]
            if corrected and corrected != word:
                explanation = f"拼写可能有误，建议改为 {corrected}"
            else:
                explanation = "拼写可能有误，请核对"
            detections.append(
                Detection(
                    original=word,
                    corrected=corrected,
                    type="hard",
                    tier="error",
                    explanation_zh=explanation,
                    source="spell",
                    error_code="SPELL",  # 本地拼写命中确定是拼写问题，代码直接盖（D19）
                    start=match.start(),
                    end=match.end(),
                )
            )
        return detections
