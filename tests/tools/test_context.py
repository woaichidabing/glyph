"""locate_context 的边界用例（设计文档 §1.8 的 BC-1 ~ BC-16 + 算法 L0 ~ L7）。

不联网（R6）。这里钉死的是"给模型看的上下文必须是局部的、且是原文原样子串"。
"""

from __future__ import annotations

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.context import SENTENCE_END, locate_context

# 够长（>=8 词）的句子，用来验证"停在整句"这条主路径
LONG_SENTENCE = "He go to school every day with his friends in the morning."
TEXT = f"Yesterday I stayed at home. {LONG_SENTENCE} It was a nice day."


def span_of(text: str, fragment: str) -> tuple[int, int]:
    """用片段在 text 中的真实位置当锚点（别手算索引，手算必错）。"""
    pos = text.index(fragment)
    return pos, pos + len(fragment)


class TestBC1BasicSentence:
    def test_取到包含锚点的整句(self) -> None:
        start, end = span_of(TEXT, "go")
        ctx, scope = locate_context(TEXT, start, end)

        assert scope == "sentence"
        assert ctx == LONG_SENTENCE
        assert "Yesterday" not in ctx
        assert "nice day" not in ctx


class TestBC2AndBC3Edges:
    def test_开头第一句左界是零(self) -> None:
        text = "He go to school every day with his friends in the morning. Next one."
        start, end = span_of(text, "go")
        ctx, _ = locate_context(text, start, end)

        assert ctx.startswith("He go")

    def test_文末没有句号右界到结尾(self) -> None:
        text = "Yesterday I stayed at home. He go to school every day with his friends"
        start, end = span_of(text, "go")
        ctx, scope = locate_context(text, start, end)

        assert scope == "sentence"
        assert ctx.endswith("friends")


class TestBC4AndBC5Closers:
    def test_句末引号跟着一起带走(self) -> None:
        text = 'He said loudly that "I am very happy today". Then he went back home.'
        start, end = span_of(text, "happy")
        ctx, scope = locate_context(text, start, end)

        assert scope == "sentence"
        assert ctx.endswith('today".')
        assert "Then" not in ctx

    def test_句末括号跟着一起带走(self) -> None:
        text = "We counted the animals in the figure (see Fig. 1). Then we went home."
        start, end = span_of(text, "counted")
        ctx, _ = locate_context(text, start, end)

        assert ctx.endswith("(see Fig. 1).")
        assert "Then" not in ctx


class TestBC6ToBC9Abbreviations:
    def test_Mr_不切断(self) -> None:
        text = "I met Mr. Smith and we walked to the park together yesterday afternoon."
        start, end = span_of(text, "Smith")
        ctx, scope = locate_context(text, start, end)

        assert scope == "sentence"
        assert "Mr. Smith" in ctx

    def test_eg_不切断(self) -> None:
        text = "We like animals, e.g. cats and dogs, and we keep two at home today."
        start, end = span_of(text, "cats")
        ctx, _ = locate_context(text, start, end)

        assert "e.g. cats" in ctx

    def test_US_不切断(self) -> None:
        text = "In the U.S. people drive on the right side of the road every single day."
        start, end = span_of(text, "drive")
        ctx, _ = locate_context(text, start, end)

        assert "U.S." in ctx

    def test_连续标点一次吞完(self) -> None:
        text = "Really?! He go to school every day with his friends in the morning."
        start, end = span_of(text, "go")
        ctx, scope = locate_context(text, start, end)

        assert scope == "sentence"
        assert "Really" not in ctx


class TestBC10AndBC11Paragraphs:
    def test_单个换行算句末_碎片句升段落(self) -> None:
        text = "He go to\nschool every day. Yesterday I stayed at home with my mum."
        start, end = span_of(text, "go")
        ctx, scope = locate_context(text, start, end)

        # \n 是句子结束符 → 切出来的句子很短 → 按 L5b 升段落
        assert scope == "paragraph"

    def test_升级时扩到空行边界_不跨段(self) -> None:
        text = "First paragraph is here and it is long enough.\n\nSecond one stays separate."
        start, end = span_of(text, "here")
        ctx, scope = locate_context(text, start, end, min_words=999)

        assert scope == "paragraph"
        assert ctx == "First paragraph is here and it is long enough."
        assert "Second" not in ctx


class TestBC12ToBC14Anchoring:
    def test_定位不到时用原文找_并直接给段落(self) -> None:
        start, end = span_of(TEXT, "go")
        ctx, scope = locate_context(TEXT, None, None, original="go")

        assert scope == "paragraph"
        assert "go" in ctx

    def test_原文也找不到就抛错_不给全文(self) -> None:
        with pytest.raises(ToolError, match="找不到原文片段"):
            locate_context(TEXT, None, None, original="压根不存在的片段")

    def test_没给区间也没给原文也抛错(self) -> None:
        with pytest.raises(ToolError, match="找不到原文片段"):
            locate_context(TEXT, None, None)

    def test_区间越界夹回来_不抛错(self) -> None:
        ctx, _ = locate_context(TEXT, 0, 99999)

        assert ctx.endswith("day.")
        assert len(ctx) <= len(TEXT)


class TestBC15AndBC16Degenerate:
    def test_极短文本升段落等于全文(self) -> None:
        text = "It was fun."
        ctx, scope = locate_context(text, 0, 4)

        assert scope == "paragraph"
        assert ctx == text

    def test_空文本抛错(self) -> None:
        with pytest.raises(ToolError, match="文本为空"):
            locate_context("", 0, 1)

    def test_纯空白抛错(self) -> None:
        with pytest.raises(ToolError, match="文本为空"):
            locate_context("   ", 0, 1)


class TestL6HardCap:
    def test_超长时按句尾左截并记提示(self) -> None:
        # 用 min_words=999 逼出段落级（BC-15 要求硬顶同样保护段落）
        text = " ".join([LONG_SENTENCE] * 10)
        start, end = span_of(text, "go")
        warnings: list[str] = []
        ctx, scope = locate_context(
            text, start, end, min_words=999, max_chars=120, warnings=warnings
        )

        assert scope == "paragraph"
        assert len(ctx) <= 120
        assert ctx.endswith(".")  # 按句尾截，不留半句话
        assert any("截断" in w for w in warnings)

    def test_没超顶就不记提示(self) -> None:
        start, end = span_of(TEXT, "go")
        warnings: list[str] = []
        locate_context(TEXT, start, end, warnings=warnings)

        assert warnings == []


class TestL7RawSlice:
    def test_不加工原文(self) -> None:
        text = "Yesterday I stayed at home.   He go to school every day with friends.  "
        start, end = span_of(text, "go")
        ctx, scope = locate_context(text, start, end)

        assert scope == "sentence"
        assert ctx == "He go to school every day with friends."
        assert "Yesterday" not in ctx


class TestSentenceEndShared:
    """句子边界必须是全项目单点（R4），与 spell.py 同源。"""

    def test_换行算句末(self) -> None:
        assert "\n" in SENTENCE_END

    def test_包含三种句末标点(self) -> None:
        assert {".", "!", "?"} <= SENTENCE_END
