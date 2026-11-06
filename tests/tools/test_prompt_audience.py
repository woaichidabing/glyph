"""子工具角色提示词的"服务对象 / 体裁不窄化"护栏（去学生化第二轮）。

主提示词已在 identity 段分流"办事型 vs 学习型"；这里守的是三个**子模型**的工牌：
它们不能把自己窄化成"作文老师"——否则用户贴一封邮件、一条工作消息来，模型会端着
改作业的架子，甚至觉得"这不是作文"而放水。来的任何英文文本都按同一把尺子校对。

真模型批改质量由 tests/tools/test_grade_golden_integration.py（39 条金标）兜底，
这里只离线锁定角色定位与体裁范围不被回退。
"""

from glyph.tools.prompts import RECHECK_SYSTEM_PROMPT
from glyph.tools.prompts import SYSTEM_PROMPT as GRADE_PROMPT
from glyph.tools.writing_analyzer import SYSTEM_PROMPT as WRITING_PROMPT


def test_grade_role_is_generic_writing_not_essay_teacher():
    """批改子模型：英语写作批改，接任何英文文体，不端老师架子、不假设是作业。"""
    assert "作文批改老师" not in GRADE_PROMPT
    assert "英语写作批改" in GRADE_PROMPT
    # 服务所有人的定位必须保留
    assert "服务的是所有" in GRADE_PROMPT
    # 明确各种实用文体都接、同一把尺子、不挑体裁不放水
    assert "邮件" in GRADE_PROMPT
    assert "不挑体裁" in GRADE_PROMPT


def test_recheck_role_is_an_independent_reviewer_not_teacher():
    """复核子模型：独立复核员（立场中立），不是"复核老师"。"""
    assert "复核老师" not in RECHECK_SYSTEM_PROMPT
    assert "复核员" in RECHECK_SYSTEM_PROMPT


def test_writing_analyzer_role_is_generic_writing_level_not_essay():
    """水平评定子模型：评的是英语写作水平，不限定作文。"""
    assert "作文等级评定老师" not in WRITING_PROMPT
    assert "写作水平" in WRITING_PROMPT
