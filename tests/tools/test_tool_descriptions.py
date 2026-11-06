"""工具描述的「什么时候别调」守护测试。

背景（2026-09-25 实测）：用户只发一张图、一句话没说，前端就写死注入
「请转写这张图片」，于是每张图都进 OCR 工作台。复盘得到的根因不是模型理解错，
而是**工具描述只写了「什么时候该调」，没写「什么时候别调」** —— 模型没有边界可依。

横向取证：Claude Code 与 Codex 都靠工具描述里的 "when to use" 让模型选对工具
（Codex 模块表里根本没有 intent / router 模块）。我们缺的正是这一半。

这里只离线锁定两件事：
1. 每个工具都必须写清「什么时候别调」（不能只写该调）；
2. ocr_image 按项目决策**绑进批改链路** —— 只在要批改图里的英文时才调。
"""

from __future__ import annotations

import pytest

from glyph.tools import DescribeImageTool, GradeTextTool, OcrImageTool, RecheckDetectionTool
from glyph.tools.analyze_writing import AnalyzeWritingTool
from glyph.tools.attempt_tools import DeleteAttemptTool, ReadAttemptsTool, RecordAttemptTool
from glyph.tools.file_tools import ReadFileTool
from glyph.tools.lookup_word import LookupWordTool
from glyph.tools.memory_tools import ForgetTool, ReadMemoryTool, RememberTool
from glyph.tools.polish import PolishTextTool
from glyph.tools.wordbook_proposal import ProposeWordbookAddTool

_ALL_TOOLS = [
    GradeTextTool,
    OcrImageTool,
    RecheckDetectionTool,
    DescribeImageTool,
    RememberTool,
    ForgetTool,
    ReadMemoryTool,
    # 后加的三个能力（词典 / 读文件 / 定级）同样必须写清「什么时候别调」
    LookupWordTool,
    ReadFileTool,
    AnalyzeWritingTool,
    # T07 学习尝试记录三件套：确认才写、练习才读、点名才删
    RecordAttemptTool,
    ReadAttemptsTool,
    DeleteAttemptTool,
    # 表达升档：只在"英文挑不出错、只想写得更好"时才调，别抢 grade_text 的活
    PolishTextTool,
    # 收词卡：只在用户可能想记这个词时递，工具本身绝不写生词本
    ProposeWordbookAddTool,
]

# 「别调」的信号词。三家横向对比下来，光写"该调"不够，模型需要一个明确的禁区。
_NOT_CALL_MARKERS = ("不要用于", "不要调用")

# 「该调」的信号词。⚠️ 不能用 "调用" 当判据——"不要调用"里也有"调用"，
# 一条纯负面描述能同时满足"该调"和"别调"两条断言，守护等于没写。
_WHEN_CALL_MARKERS = ("当且仅当", "时才调用")


@pytest.mark.parametrize("tool", _ALL_TOOLS, ids=lambda t: t.name)
def test_description_also_says_when_not_to_call(tool):
    """每个工具的描述都必须写清「什么时候别调」，不能只写什么时候该调。

    只写"该调"时，模型面对模糊输入（比如用户只发一张图）没有禁区可依，
    就会往它认识的那个工具上靠 —— 这正是「发图就进 OCR 工作台」的成因。
    """
    assert any(m in tool.description for m in _NOT_CALL_MARKERS), tool.description


@pytest.mark.parametrize("tool", _ALL_TOOLS, ids=lambda t: t.name)
def test_description_still_says_when_to_call(tool):
    """补了「别调」不能把「该调」挤掉 —— 两头都要在。

    ⚠️ 判据必须是**正向**标记（"当且仅当" / "时才调用"）。
    这条原来断言 ``"调用" in description``，而"不要调用它"里同样有"调用"——
    于是"别调"写得越全，这条越容易被蒙混过关，是条假守护。
    """
    assert any(m in tool.description for m in _WHEN_CALL_MARKERS), tool.description


def test_ocr_image_is_bound_to_the_grading_flow():
    """ocr_image 只在「要批改这张图里的英文」时才调（人 2026-09-25 拍板）。

    它和批改是绑定的，但批改（grade_text）可以单独用 —— 纯文本批改跟图无关。
    """
    assert "要批改这张图里的英文" in OcrImageTool.description
    assert "没说要批改时不要调用" in OcrImageTool.description


def test_ocr_image_keeps_transcribe_only_contract():
    """绑进批改链路不能改变它的职责闸：仍然只转写、绝不批改。"""
    assert "不做批改" in OcrImageTool.description
    assert "[?]" in OcrImageTool.description


def test_describe_image_is_the_look_only_entry():
    """describe_image 是「看懂图」的轻量入口：只看不批，跟 ocr_image 分工明确。

    两个工具都吃 image_id、都调同一个视觉模型，描述里必须把界线画清，
    否则模型会选错（工具越多越容易选错，这正是人最担心的那件事）。
    """
    assert "只看不批" in DescribeImageTool.description
    assert "不做批改" in DescribeImageTool.description
    # 要批改图里的英文 → 那是 ocr_image 的活，不能在看图工具里顺手做掉
    assert "ocr_image" in DescribeImageTool.description


def test_grade_text_refuses_unconfirmed_transcript():
    """跟系统词 grading_line 那道铁律同源：没经用户确认的转写稿不许拿去批。"""
    assert "未经用户确认" in GradeTextTool.description


def test_recheck_detection_is_only_for_human_rejection():
    """复核只能由「老师点驳回」触发，模型不能自己主动复核整篇。"""
    assert "当且仅当" in RecheckDetectionTool.description
    assert "主动复核" in RecheckDetectionTool.description


def test_polish_text_does_not_steal_grading():
    """升档与批改的分工必须写在描述里 —— 工具越多越容易选错。

    用户说"帮我改改"时，模型必须知道那是有错的活（grade_text），
    而不是把它当升档；反过来"帮我写得更好"也不该进批改。
    """
    assert "grade_text" in PolishTextTool.description
    assert "先把错改对" in PolishTextTool.description
    assert "不要用于" in PolishTextTool.description
