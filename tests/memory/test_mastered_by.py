"""掌握方式标记（D31）：区分「复测连对达标」与「用户自述 + 验证通过」。

两种都算数，但以后回看时能分清这条依据有多硬。
"""

from datetime import date

from glyph.memory.mastery import (
    DEFAULT_SCHEDULE,
    MasteryPoint,
    MasteryStage,
    ReviewVerdict,
    record_verdict,
    record_user_claim,
)

_TODAY = date(2026, 10, 15)



def point(**overrides) -> MasteryPoint:
    kwargs = dict(
        key="aaaaaaaaaaaa",
        error_code="VERB:TENSE",
        first_seen=date(2026, 10, 1),
        stage=MasteryStage.LEARNING,
        streak=DEFAULT_SCHEDULE.mastery_streak - 1,  # 再来一次独立对就已掌握
        due_date=_TODAY,
        good_count=2,
        hard_count=0,
        wrong_count=0,
        last_review_at=None,
        reopened_count=0,
    )
    kwargs.update(overrides)
    return MasteryPoint(**kwargs)



def test_streak_mastery_is_marked_as_streak() -> None:
    """连对达标为已掌握 → mastered_by = streak。"""
    out = record_verdict(point(), ReviewVerdict.INDEPENDENT, _TODAY)
    assert out.stage is MasteryStage.MASTERED
    assert out.mastered_by == "streak"



def test_user_claim_mastery_is_marked_as_user_claim() -> None:
    """用户说到、出一道题答对 → mastered_by = user_claim。"""
    out = record_user_claim(point(), _TODAY)
    assert out.stage is MasteryStage.MASTERED
    assert out.mastered_by == "user_claim"
    assert out.due_date is None



def test_user_claim_requires_correct_answer() -> None:
    """答错了不算——保持原状态，阶梯继续（D31：答错不标为已掌握）。"""
    before = point()
    out = record_user_claim(before, _TODAY, verified=False)

    assert out.stage is not MasteryStage.MASTERED
    assert out.mastered_by is None
    assert out.streak == before.streak  # 连对没被加



def test_unverified_claim_does_not_advance_good_count() -> None:
    """没验证通过就不该算一次"做对"。"""
    out = record_user_claim(point(good_count=5), _TODAY, verified=False)
    assert out.good_count == 5



def test_learning_verdict_has_no_mastered_by() -> None:
    """还没掌握的中间状态不带 mastered_by。"""
    out = record_verdict(point(streak=0), ReviewVerdict.INDEPENDENT, _TODAY)
    assert out.stage is MasteryStage.LEARNING
    assert out.mastered_by is None



def test_reopened_point_loses_mastered_by() -> None:
    """复活（掌握之后又犯）→ 不再是 mastered，标记也要清掉。"""
    from glyph.memory.mastery import reopen

    mastered = record_user_claim(point(), _TODAY)
    back = reopen(mastered, _TODAY)

    assert back.stage is MasteryStage.LEARNING
    assert back.mastered_by is None
    assert back.reopened_count == 1

