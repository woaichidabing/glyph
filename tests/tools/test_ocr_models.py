"""OCR 转写地基的离线测试：数据模型、[?] 扫描、转写提示词契约。

全部不联网（R6）：这里测的都是确定性函数与字符串契约，真视觉模型调用走
``integration`` marker（见后续 T02 的 test_ocr_integration.py）。
"""

from __future__ import annotations

from glyph.tools.ocr_models import (
    PLACEHOLDER,
    Transcription,
    find_low_confidence,
    unspell_transcription,
)
from glyph.tools.prompts import OCR_PROMPT, build_ocr_messages


class TestFindLowConfidence:
    def test_没有占位返回空列表(self) -> None:
        assert find_low_confidence("Last weekend was happy.") == []

    def test_空字符串返回空列表(self) -> None:
        assert find_low_confidence("") == []

    def test_单个占位的区间正确(self) -> None:
        text = "I went to [?] park."
        spans = find_low_confidence(text)

        assert len(spans) == 1
        span = spans[0]
        assert (span.start, span.end) == (10, 13)
        assert text[span.start : span.end] == PLACEHOLDER
        assert span.text == PLACEHOLDER
        assert span.reason

    def test_多个占位按出现顺序(self) -> None:
        text = "[?] went to the [?] with [?]."
        spans = find_low_confidence(text)

        assert len(spans) == 3
        assert [s.start for s in spans] == [0, 16, 25]
        # 每个 span 都必须能原样切回占位符，前端按 start/end 高亮才不会错位
        for s in spans:
            assert text[s.start : s.end] == PLACEHOLDER

    def test_占位在开头和结尾(self) -> None:
        text = "[?]tail[?]"
        spans = find_low_confidence(text)

        assert [(s.start, s.end) for s in spans] == [(0, 3), (7, 10)]

    def test_相邻占位各自独立(self) -> None:
        spans = find_low_confidence("[?][?]")

        assert len(spans) == 2
        assert [s.start for s in spans] == [0, 3]


class TestTranscription:
    def test_只有必填字段时其余为空(self) -> None:
        t = Transcription(text="Last weekend was happy.")

        assert t.low_confidence == []
        assert t.warnings == []

    def test_可序列化为字典(self) -> None:
        t = Transcription(text="a [?] b", low_confidence=find_low_confidence("a [?] b"))
        dumped = t.model_dump()

        assert dumped["text"] == "a [?] b"
        assert len(dumped["low_confidence"]) == 1


class TestUnspellTranscription:
    """逐字母转写（b-u-y-e-d）还原成正常文本（#98 攻克 buyed->bought 的后处理）。

    对模型没遵守格式、直接输出的正常句子必须近似幂等（只归一意外多空格），
    这样无论模型是否逐字母输出都不会被破坏。
    """

    def test_逐字母拼写还原成词(self) -> None:
        assert unspell_transcription("b-u-y-e-d t-w-o") == "buyed two"

    def test_大小写逐字母保留(self) -> None:
        assert unspell_transcription("L-a-s-t w-e-e-k-e-n-d") == "Last weekend"

    def test_撇号词还原(self) -> None:
        assert unspell_transcription("d-o-n-'-t g-o") == "don't go"

    def test_词间多空格压成一个(self) -> None:
        assert unspell_transcription("Last   weekend") == "Last weekend"

    def test_保留换行(self) -> None:
        assert unspell_transcription("I g-o\nh-e") == "I go\nhe"

    def test_标点前多余空格去掉(self) -> None:
        assert unspell_transcription("friend .") == "friend."

    def test_正常文本幂等不变(self) -> None:
        assert unspell_transcription("I buyed two apple.") == "I buyed two apple."

    def test_真实连字符词不被误拆(self) -> None:
        assert unspell_transcription("an x-ray image") == "an x-ray image"

    def test_拼写与正常词混合也能还原(self) -> None:
        assert unspell_transcription("I b-u-y-e-d apples") == "I buyed apples"

    def test_B_一词一行还原成整句(self) -> None:
        raw = "L-a-s-t\nw-e-e-k-e-n-d\nI\nb-u-y-e-d\nt-w-o\nf-r-i-e-n-d."
        assert unspell_transcription(raw) == "Last weekend I buyed two friend."

    def test_B_空行分段保留段落换行(self) -> None:
        raw = "I\ng-o\nt-o\n\nH-e\nh-a-v-e"
        assert unspell_transcription(raw) == "I go to\nHe have"

    def test_B_标点独占一行粘回前词(self) -> None:
        raw = "t-h-e-r-e\n.\nT-h-e-y\nr-u-n\n."
        assert unspell_transcription(raw) == "there. They run."


class TestHalfCompliantCleanup:
    """模型半守不守逐字母格式时的确定性兜底（2026-10-13 真机：打印体输入）。

    打印体上模型不肯逐字母拼，却把提示词例子里的「 / 」分隔符照抄下来，
    标点前还粘了半个连字符（Nowadays-,）——这些残渣会漏进转写稿。
    """

    def test_光杆斜杠分隔符剔除(self) -> None:
        raw = "Nowadays-, / technology / develop / very / fast-, / and / robot / are / used"
        assert unspell_transcription(raw) == "Nowadays, technology develop very fast, and robot are used"

    def test_标点前粘连的连字符剥掉(self) -> None:
        assert unspell_transcription("fields-.") == "fields."
        assert unspell_transcription("life-, and convenient-.") == "life, and convenient."

    def test_真连字符词与词内斜杠不受影响(self) -> None:
        assert unspell_transcription("The x-ray image is clear") == "The x-ray image is clear"
        assert unspell_transcription("and/or") == "and/or"


class TestOcrPromptContract:
    def test_逐字母转写机制(self) -> None:
        # #98：唯一真机验证能阻止 buyed 被脑补成 bought 的办法，是把任务压到字母层；
        # 一词一行让词边界交给换行（强边界），避免模型用连字符把不同单词焊死
        lowered = OCR_PROMPT.lower()
        assert "letter by letter" in lowered
        assert "hyphen" in lowered
        assert "one word per line" in lowered

    def test_错字逐字母保留_禁止的正确词也逐字母给出(self) -> None:
        # 错例 b-u-y-e-d 必须原样输出；正确写法 b-o-u-g-h-t 被明确点名禁止
        lowered = OCR_PROMPT.lower()
        assert "b-u-y-e-d" in lowered
        assert "b-o-u-g-h-t" in lowered

    def test_纠错属独立的后续批改步骤(self) -> None:
        lowered = OCR_PROMPT.lower()
        assert "grad" in lowered
        assert "separate" in lowered
        assert "later" in lowered
        assert "not your job" in lowered

    def test_残缺词不许补全字母(self) -> None:
        # 真机：图里 lik（少字母）曾被顺着语境补成 liked；必须显式禁止"补字母"
        lowered = OCR_PROMPT.lower()
        assert "missing letters" in lowered
        assert "l-i-k" in lowered

    def test_认不出要留占位(self) -> None:
        assert "[?]" in OCR_PROMPT

    def test_明确忽略界面元素与姓名(self) -> None:
        # 冒烟实测：不加这句，模型会把 App 按钮、用户真名、分数一并抄进输出
        lowered = OCR_PROMPT.lower()
        assert "people's names" in lowered
        assert "scores" in lowered

    def test_只输出英文正文(self) -> None:
        lowered = OCR_PROMPT.lower()
        assert "no commentary" in lowered

    def test_例子不再教模型输出斜杠分隔符(self) -> None:
        # 2026-10-13 真机：打印体输入时模型把例子里的「 / 」照抄进转写稿，
        # 词间全是斜杠。格式例子不许再出现这个字符。
        assert " / " not in OCR_PROMPT

    def test_打印体直接抄整词(self) -> None:
        # 打印/印刷体文字本身无歧义，逐字母是给手写错词用的；
        # 强迫打印体也逐字母会半守不守，产出斜杠残渣。
        lowered = OCR_PROMPT.lower()
        assert "printed" in lowered
        assert "whole word" in lowered
        assert "no translation" in lowered


class TestBuildOcrMessages:
    def test_单条用户消息带图与指令(self) -> None:
        msgs = build_ocr_messages("BASE64DATA", "image/jpeg")

        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        content = msgs[0]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "image_url"
        assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert content[0]["image_url"]["url"].endswith("BASE64DATA")
        assert content[1]["type"] == "text"
        assert content[1]["text"] == OCR_PROMPT

    def test_mime_按传入值拼接(self) -> None:
        msgs = build_ocr_messages("X", "image/png")

        assert msgs[0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
