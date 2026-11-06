"""掌握度账本状态机纯函数测试（第二棒第①片）。

约定（见 docs/prd/mastery-ledger.md，乙套餐 1→3→7 天、连对 3 次即掌握）：
- 建档次日首考；第 1/2/3 次独立做对分别排 3 天后、7 天后、已掌握；
- 提示后对：不升级也不清零，停在当前档、明天再来；
- 没做对：连对清零、打回起点、明天重来；
- 掌握之后又犯：reopen 复活，回 learning、清零、次日重走；
- 到期选择：错最多优先，其次拖最久（due 最早）、最早发现、key 兜底。

这些函数不做 IO、不调模型，today 一律注入。
"""

from datetime import date, timedelta

import pytest

from glyph.memory import MemoryEntry, MemoryType
from glyph.memory.mastery import (
    CODE_LABELS_ZH,
    DEFAULT_SCHEDULE,
    VALID_ERROR_CODES,
    MasteryPoint,
    MasteryStage,
    ReviewVerdict,
    ScheduleConfig,
    apply_point_to_entry,
    enroll,
    is_due,
    normalize_error_code,
    point_from_entry,
    record_verdict,
    reopen,
    select_due,
)

D0 = date(2026, 10, 2)
VERB_TENSE = "VERB:TENSE"


# --- 5a：知识点代码白名单与规范化（D17：6 类外只批改、不追踪）---


@pytest.mark.parametrize(
    "code",
    ["VERB:TENSE", "MORPH", "NOUN:NUM", "NOUN:INFL", "DET", "SPELL"],
)
def test_normalize_accepts_the_six_whitelisted_codes(code: str) -> None:
    assert normalize_error_code(code) == code


def test_normalize_tolerates_case_and_surrounding_whitespace() -> None:
    assert normalize_error_code("  verb:tense ") == "VERB:TENSE"
    assert normalize_error_code("det") == "DET"
    assert normalize_error_code(" noun:num") == "NOUN:NUM"


@pytest.mark.parametrize(
    "raw",
    [
        "PREP",  # 介词：第一版不追踪
        "PUNCT",  # 标点
        "VERB:FORM",  # 真实 ERRANT 代码但不在 6 类（词形归 MORPH）
        "",
        "   ",
        "乱七八糟",
        None,
        123,
    ],
)
def test_normalize_rejects_out_of_scope_and_garbage(raw: object) -> None:
    assert normalize_error_code(raw) is None


def test_valid_error_codes_is_frozen_view_of_labels() -> None:
    # 白名单与中文名表是同一事实源，避免两处各维护一份
    assert VALID_ERROR_CODES == frozenset(CODE_LABELS_ZH)


def test_enroll_schedules_first_review_next_day() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)

    assert point.stage is MasteryStage.NEW
    assert point.streak == 0
    assert point.due_date == D0 + timedelta(days=1)
    assert point.first_seen == D0
    assert point.good_count == point.hard_count == point.wrong_count == 0
    assert point.last_review_at is None


def test_plan_b_three_independent_correct_then_mastered() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)
    assert point.due_date == date(2026, 10, 3)

    # 第 1 次独立做对：进入 learning、连对 1、3 天后再考
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 3))
    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 1
    assert point.due_date == date(2026, 10, 6)

    # 第 2 次独立做对：连对 2、7 天后再考
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 6))
    assert point.streak == 2
    assert point.due_date == date(2026, 10, 13)

    # 第 3 次独立做对：已掌握、不再排期
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 13))
    assert point.stage is MasteryStage.MASTERED
    assert point.streak == 3
    assert point.due_date is None
    assert point.good_count == 3
    assert point.last_review_at == date(2026, 10, 13)


def test_hinted_from_new_enters_learning_but_keeps_streak_zero() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)

    point = record_verdict(point, ReviewVerdict.HINTED, date(2026, 10, 3))

    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 0
    assert point.due_date == date(2026, 10, 4)
    assert point.hard_count == 1


def test_hinted_midway_keeps_streak_and_comes_tomorrow() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 3))
    assert point.streak == 1

    point = record_verdict(point, ReviewVerdict.HINTED, date(2026, 10, 6))

    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 1  # 不升级也不清零
    assert point.due_date == date(2026, 10, 7)
    assert point.hard_count == 1
    assert point.good_count == 1


def test_wrong_resets_streak_and_returns_to_start() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 3))
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 6))
    assert point.streak == 2

    point = record_verdict(point, ReviewVerdict.WRONG, date(2026, 10, 13))

    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 0
    assert point.due_date == date(2026, 10, 14)
    assert point.wrong_count == 1
    assert point.good_count == 2  # 历史累计保留


def test_wrong_on_first_review_enters_learning_at_zero() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)

    point = record_verdict(point, ReviewVerdict.WRONG, date(2026, 10, 3))

    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 0
    assert point.due_date == date(2026, 10, 4)
    assert point.wrong_count == 1


def test_reopen_after_mastered_restarts_learning() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)
    for day in (date(2026, 10, 3), date(2026, 10, 6), date(2026, 10, 13)):
        point = record_verdict(point, ReviewVerdict.INDEPENDENT, day)
    assert point.stage is MasteryStage.MASTERED

    revived = reopen(point, date(2026, 11, 1))

    assert revived.stage is MasteryStage.LEARNING
    assert revived.streak == 0
    assert revived.due_date == date(2026, 11, 2)
    assert revived.reopened_count == 1
    assert revived.first_seen == D0  # 最初发现日保留
    assert revived.good_count == 3  # 历史累计不清零


def test_record_verdict_on_mastered_is_rejected() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)
    for day in (date(2026, 10, 3), date(2026, 10, 6), date(2026, 10, 13)):
        point = record_verdict(point, ReviewVerdict.INDEPENDENT, day)

    with pytest.raises(ValueError):
        record_verdict(point, ReviewVerdict.WRONG, date(2026, 11, 1))


@pytest.mark.parametrize(
    ("due", "stage", "expected"),
    [
        (D0, MasteryStage.LEARNING, True),  # 今天到期
        (D0 - timedelta(days=2), MasteryStage.LEARNING, True),  # 逾期
        (D0 + timedelta(days=1), MasteryStage.LEARNING, False),  # 明天才到期
        (D0 - timedelta(days=2), MasteryStage.MASTERED, False),  # 已掌握不再提醒
        (None, MasteryStage.MASTERED, False),
    ],
)
def test_is_due(due: date | None, stage: MasteryStage, expected: bool) -> None:
    point = MasteryPoint(key="k", error_code=VERB_TENSE, first_seen=D0, stage=stage, due_date=due)
    assert is_due(point, D0) is expected


def test_select_due_filters_not_due_and_mastered() -> None:
    due_today = MasteryPoint(
        key="due-today",
        error_code="DET",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0,
        wrong_count=0,
    )
    not_yet = MasteryPoint(
        key="future",
        error_code="SPELL",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0 + timedelta(days=3),
        wrong_count=5,
    )
    mastered = MasteryPoint(
        key="done",
        error_code="MORPH",
        first_seen=D0,
        stage=MasteryStage.MASTERED,
        due_date=D0 - timedelta(days=9),
        wrong_count=9,
    )

    chosen = select_due([due_today, not_yet, mastered], D0)

    assert [p.key for p in chosen] == ["due-today"]


def test_select_due_prefers_most_wrong_then_longest_overdue() -> None:
    # 同日到期：错得更多的优先
    a = MasteryPoint(
        key="a",
        error_code="NOUN:NUM",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0,
        wrong_count=1,
    )
    b = MasteryPoint(
        key="b",
        error_code="NOUN:INFL",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0,
        wrong_count=4,
    )
    assert [p.key for p in select_due([a, b], D0)] == ["b"]

    # 错得一样多：拖最久（due 最早）的优先
    c = MasteryPoint(
        key="c",
        error_code="DET",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0 - timedelta(days=5),
        wrong_count=2,
    )
    d = MasteryPoint(
        key="d",
        error_code="SPELL",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0 - timedelta(days=1),
        wrong_count=2,
    )
    assert [p.key for p in select_due([c, d], D0)] == ["c"]

    # 默认每次只取 1 个
    assert len(select_due([a, b, c, d], D0, limit=1)) == 1


def test_select_due_empty_when_nothing_due() -> None:
    point = MasteryPoint(
        key="future",
        error_code="SPELL",
        first_seen=D0,
        stage=MasteryStage.LEARNING,
        due_date=D0 + timedelta(days=2),
    )
    assert select_due([point], D0) == []
    assert select_due([], D0) == []


def test_config_switches_to_plan_a() -> None:
    plan_a = ScheduleConfig(first_review_days=1, good_intervals=(3,), mastery_streak=3 - 1)

    point = enroll("k", VERB_TENSE, D0, config=plan_a)
    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 3), config=plan_a)
    assert point.streak == 1
    assert point.due_date == date(2026, 10, 6)

    point = record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 6), config=plan_a)
    assert point.stage is MasteryStage.MASTERED  # 连对 2 次即已掌握
    assert point.due_date is None


def test_invalid_config_interval_length_rejected() -> None:
    # mastery_streak=3 需要 2 个 good 间隔（streak 1、2 各一个）
    with pytest.raises(ValueError):
        ScheduleConfig(mastery_streak=3, good_intervals=(3,))


def test_record_verdict_does_not_mutate_input() -> None:
    point = enroll("verb-tense", VERB_TENSE, D0)
    before = (point.stage, point.streak, point.due_date, point.good_count)

    record_verdict(point, ReviewVerdict.INDEPENDENT, date(2026, 10, 3))

    assert (point.stage, point.streak, point.due_date, point.good_count) == before


def test_default_schedule_is_plan_b() -> None:
    assert DEFAULT_SCHEDULE.mastery_streak == 3
    assert DEFAULT_SCHEDULE.good_intervals == (3, 7)
    assert DEFAULT_SCHEDULE.first_review_days == 1
    assert DEFAULT_SCHEDULE.retry_days == 1


# ---------- MemoryEntry ↔ MasteryPoint 映射（第②片持久化桥梁）----------


def _mastery_entry(**overrides) -> MemoryEntry:
    base = dict(
        key="cafe00000001",
        type=MemoryType.MISTAKE,
        title="一般过去时",
        quote="yesterday I go home",
        count=1,
        first_seen="2026-10-02",
        last_seen="2026-10-02",
        last_mentioned_at="2026-10-02",
        error_code=VERB_TENSE,
        mastery_stage="learning",
        independent_streak=1,
        due_date="2026-10-06",
        good_count=1,
        hard_count=0,
        wrong_count=0,
        last_review_at="2026-10-03",
        reopened_count=0,
    )
    base.update(overrides)
    return MemoryEntry(**base)


def test_point_from_learning_entry() -> None:
    point = point_from_entry(_mastery_entry())

    assert point is not None
    assert point.key == "cafe00000001"
    assert point.error_code == VERB_TENSE
    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 1
    assert point.due_date == date(2026, 10, 6)
    assert point.good_count == 1
    assert point.last_review_at == date(2026, 10, 3)


def test_point_from_mastered_entry_has_no_due() -> None:
    point = point_from_entry(
        _mastery_entry(mastery_stage="mastered", independent_streak=3, due_date=None)
    )

    assert point is not None
    assert point.stage is MasteryStage.MASTERED
    assert point.streak == 3
    assert point.due_date is None
    assert point.reopened_count == 0


def test_point_from_unenrolled_legacy_entry_is_none() -> None:
    # v1 旧错题没有 mastery_stage：未建档，不进到期队列
    assert point_from_entry(_mastery_entry(mastery_stage=None)) is None


def test_point_from_non_mistake_is_none() -> None:
    assert point_from_entry(_mastery_entry(type=MemoryType.PREFERENCE)) is None


def test_point_from_entry_without_code_is_none() -> None:
    assert point_from_entry(_mastery_entry(error_code=None)) is None


def test_point_from_entry_bad_due_date_is_none() -> None:
    assert point_from_entry(_mastery_entry(due_date="not-a-date")) is None


# ---------- 第④片：把推进后的状态写回记忆条目（entry↔MasteryPoint 反向桥梁）----------


def test_apply_point_writes_back_ledger_and_keeps_other_fields() -> None:
    entry = _mastery_entry()  # 已建档 learning、连对 1
    point = point_from_entry(entry)
    assert point is not None
    advanced = record_verdict(point, ReviewVerdict.INDEPENDENT, D0)  # 又独立对一次

    out = apply_point_to_entry(advanced, entry)

    assert out is not entry  # 返回副本，不原地改
    # 账本 8 字段按推进结果回写（date 转 ISO 字符串）
    assert out.mastery_stage == advanced.stage.value
    assert out.error_code == advanced.error_code
    assert out.independent_streak == advanced.streak
    assert out.due_date == advanced.due_date.isoformat()
    assert out.good_count == advanced.good_count
    assert out.hard_count == advanced.hard_count
    assert out.wrong_count == advanced.wrong_count
    assert out.last_review_at == advanced.last_review_at.isoformat()
    assert out.reopened_count == advanced.reopened_count
    # 账本之外的身份 / 正文 / 计数 / 日期原样保留，不丢字段
    assert out.key == entry.key
    assert out.title == entry.title
    assert out.original == entry.original
    assert out.correction == entry.correction
    assert out.issue_type == entry.issue_type
    assert out.tier == entry.tier
    assert out.count == entry.count
    assert out.first_seen == entry.first_seen
    assert out.last_seen == entry.last_seen
    # 传入的旧条目不被原地修改
    assert entry.independent_streak == 1
    assert entry.good_count == 1


def test_apply_point_mastered_clears_due_date() -> None:
    entry = _mastery_entry(mastery_stage="learning", independent_streak=2, good_count=2)
    point = point_from_entry(entry)
    assert point is not None
    mastered = record_verdict(point, ReviewVerdict.INDEPENDENT, D0)  # 第 3 次独立对已掌握
    assert mastered.stage is MasteryStage.MASTERED

    out = apply_point_to_entry(mastered, entry)

    assert out.mastery_stage == "mastered"
    assert out.independent_streak == 3
    assert out.due_date is None  # 掌握之后不再排到期
    assert out.last_review_at == D0.isoformat()
