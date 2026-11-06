"""T06 学习状态摘要的渲染测试（后端侧）。

每条对着一个会真实发生的坑写：
- 没数据却渲染出一段"进度"，Agent 会据此说出没依据的话
- 摘要里出现可回写的字段（reps / lapses 明细），等于给 Agent 开改写口子
- 没有反复遗忘的词却硬写一行"无"，把"没有"说成"查过是零"
"""

from __future__ import annotations

from glyph.context.study import (
    StudyContext,
    render_study_context,
    study_fragment,
)


def ctx(**over: object) -> StudyContext:
    base: StudyContext = {
        "book": "中考核心词",
        "mode": "plan",
        "due": 4,
        "newTotal": 3,
        "newDone": 2,
        "reviewTotal": 2,
        "reviewDone": 1,
        "hardWords": ["w4", "w1"],
    }
    base.update(over)  # type: ignore[arg-type]
    return base


def test_no_data_renders_nothing() -> None:
    """没选词书 = 没有学习状态。返回 None，后端据此不注入这一段。"""
    assert render_study_context(None) is None


def test_with_data_mentions_book_and_due() -> None:
    text = render_study_context(ctx())
    assert text is not None
    assert "中考核心词" in text
    assert "4" in text


def test_progress_is_rendered() -> None:
    text = render_study_context(ctx(newTotal=3, newDone=2, reviewTotal=2, reviewDone=1)) or ""
    assert "2/3" in text
    assert "1/2" in text


def test_no_hard_words_omits_the_line() -> None:
    """没有反复遗忘的词就不提这一项：不写"无"，免得把"没有记录"说成"查过是零"。"""
    text = render_study_context(ctx(hardWords=[])) or ""
    assert "反复遗忘" not in text


def test_hard_words_are_listed() -> None:
    text = render_study_context(ctx(hardWords=["w4", "w1"])) or ""
    assert "w4" in text and "w1" in text


def test_readonly_is_stated() -> None:
    """必须写明只读：摘要只供参考，改 SRS / 计划仍由背词页或正式写入能力做。"""
    text = render_study_context(ctx()) or ""
    assert "只读" in text


def test_no_writable_fields_leak() -> None:
    """reps / lapses / dueDate 这类可回写字段不许出现在摘要里。"""
    text = render_study_context(ctx()) or ""
    assert "reps" not in text and "lapses" not in text and "dueDate" not in text


def test_fragment_returns_none_without_data() -> None:
    assert study_fragment(lambda: None)() is None


def test_fragment_renders_when_data_present() -> None:
    frag = study_fragment(lambda: ctx())
    assert frag() is not None


def test_fragment_follows_changes() -> None:
    """片段是回调：数据变了（用户刚背完几个词）下一轮就得是新的。"""
    data: list[StudyContext | None] = [ctx(due=0)]
    frag = study_fragment(lambda: data[0])
    first = frag()
    data[0] = ctx(due=9)
    assert frag() != first
