"""复核提示词的**立场纪律**（反谄媚）离线守护。

不联网（R6）。这里钉死一句话：**「被要求复核」不等于「AI 错了」**。

背景（真实走过一次的坑）：初版提示词开头写「正在复核一条已被老师驳回的批改意见」，
等于把"老师认为你错了"这个立场直接喂给模型；再叠加「拿不准就 withdrawn」这条铁律，
模型就顺着提异议的人改口——老师驳什么 AI 都认错，复核退化成橡皮图章，
与「驳回 = 请 AI 真的再看一遍」的产品语义正好相反（用户的原话：
「驳回的时候千万不能顺着用户，要真实的去查询」）。

因此这里守两组东西：**不该出现的立场污染串**、**必须出现的反谄媚与 N-1 串**。
"""

from __future__ import annotations

from glyph.tools.grading_models import Detection, Source
from glyph.tools.prompts import RECHECK_SYSTEM_PROMPT, build_recheck_messages
from glyph.tools.recheck import RecheckRequest

TEXT = "He go to school every day with his friends."

# 立场污染串：只要出现，就是在教模型"顺着提异议的人改口"
# "已被老师驳回" 是 "被老师驳回" 的超集（后者是它的子串），不必重复列
POLLUTED = ("被老师驳回", "老师认为你错了", "你错了，请改正")


def _req(round_: int = 1, source: Source = "model") -> RecheckRequest:
    start = TEXT.index("go")
    return RecheckRequest(
        detection=Detection(
            id="d3",
            original="go",
            corrected="went",
            type="hard",
            tier="error",
            explanation_zh="过去的事要用过去式",
            start=start,
            end=start + 2,
            aligned=True,
            source=source,
        ),
        context_text=TEXT,
        scope="sentence",
        round=round_,
    )


def _system(round_: int = 1, source: str = "model") -> str:
    return build_recheck_messages(_req(round_, source))[0]["content"]


def _user(round_: int = 1) -> str:
    return build_recheck_messages(_req(round_))[1]["content"]


class TestNoSycophancy:
    """不该出现的：任何"你已经错了"的暗示。"""

    def test_系统词不含被驳回的立场暗示(self) -> None:
        for bad in POLLUTED:
            assert bad not in RECHECK_SYSTEM_PROMPT

    def test_用户消息不含被驳回的立场暗示(self) -> None:
        for bad in POLLUTED:
            assert bad not in _user()

    def test_拼写口径追加后也不含立场暗示(self) -> None:
        system = _system(source="spell")

        assert "本地拼写词典" in system
        for bad in POLLUTED:
            assert bad not in system


class TestAntiSycophancyPresent:
    """必须出现的：明说了"不许顺着改口"。"""

    def test_明写不许顺着改口(self) -> None:
        assert "不许顺着改口" in RECHECK_SYSTEM_PROMPT

    def test_明写被复核不等于你错了(self) -> None:
        assert "不等于你错了" in RECHECK_SYSTEM_PROMPT

    def test_维持原判被写成正当答案(self) -> None:
        assert "维持原判和撤销一样是被期待的答案" in RECHECK_SYSTEM_PROMPT

    def test_反谄媚是零号铁律且排在最前(self) -> None:
        i0 = RECHECK_SYSTEM_PROMPT.index("0. **独立判断")
        i1 = RECHECK_SYSTEM_PROMPT.index("1. **只判断指定的这一处")

        assert i0 < i1


class TestUnknownStillWithdrawn:
    """N-1 的初衷（保护用户，误报比漏判更伤人）不能因为反谄媚被丢掉。"""

    def test_仍保留拿不准就撤(self) -> None:
        assert "拿不准就 withdrawn" in RECHECK_SYSTEM_PROMPT

    def test_拿不准被限定为读完句子之后(self) -> None:
        assert "读完句子之后" in RECHECK_SYSTEM_PROMPT

    def test_明说顺着改口不算拿不准(self) -> None:
        assert "那是违反第 0 条" in RECHECK_SYSTEM_PROMPT


class TestRoundIndependence:
    """多轮复核不能变成"驳两次我就该认错"。"""

    def test_二轮复核不带上一轮错了的暗示(self) -> None:
        assert "不是你上一轮错了" in _user(round_=2)

    def test_轮次照常回带(self) -> None:
        assert "这是第 2 次复核" in _user(round_=2)

    def test_一轮复核不提上一轮(self) -> None:
        """首轮根本没有上一轮，提它会凭空制造"我大概错了"的暗示。"""
        assert "不是你上一轮错了" not in _user(round_=1)
