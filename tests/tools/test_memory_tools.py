"""remember / forget / read_memory 三个记忆工具测试（真实 store、离线）。"""

from datetime import datetime, timedelta, timezone

import pytest

from glyph.memory import MemoryEntry, MemoryStore, MemoryType
from glyph.memory.mastery import MasteryStage
from glyph.runtime.errors import ToolError
from glyph.tools.memory_tools import (
    ForgetTool,
    ReadMemoryTool,
    RecordReviewResultTool,
    RememberTool,
)

_TZ = timezone(timedelta(hours=8))


def clock() -> datetime:
    return datetime(2026, 9, 25, 17, 0, 0, tzinfo=_TZ)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(root=tmp_path, clock=clock)


async def _remember(store, content, kind="preference", title=None, **extra):
    tool = RememberTool(store, title_max_chars=40)
    args = {"content": content, "kind": kind}
    if title is not None:
        args["title"] = title
    args.update(extra)
    return await tool.run(args)


async def test_remember_creates_entry_and_returns_key(store) -> None:
    result = await _remember(store, "以后讲解都用中文", title="讲解用中文")

    assert result["action"] == "created"
    assert result["kind"] == "preference"
    assert len(result["key"]) == 12
    fetched = store.get(result["key"])
    # 原话逐字保留，不被改写
    assert fetched.quote == "以后讲解都用中文"
    assert fetched.title == "讲解用中文"


async def test_remember_same_content_increments_count(store) -> None:
    first = await _remember(store, "别用 emoji")
    second = await _remember(store, "别用 emoji")

    assert second["action"] == "updated"
    assert second["key"] == first["key"]
    assert store.get(first["key"]).count == 2


async def test_remember_blank_content_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await RememberTool(store, title_max_chars=40).run({"content": "   "})


async def test_remember_invalid_kind_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await RememberTool(store, title_max_chars=40).run({"content": "x", "kind": "not-a-type"})


async def test_remember_defaults_title_from_content_and_clips(store) -> None:
    result = await _remember(store, "我希望每次批改后给我一句鼓励的话")

    fetched = store.get(result["key"])
    assert fetched.title != ""
    assert len(fetched.title) <= 40


async def test_forget_soft_deletes_entry(store) -> None:
    key = (await _remember(store, "别用 emoji"))["key"]

    result = await ForgetTool(store).run({"key": key})

    assert result["forgotten"] is True
    assert store.list_entries() == []


async def test_forget_missing_key_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await ForgetTool(store).run({"key": "000000000000"})


async def test_forget_bad_key_shape_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await ForgetTool(store).run({"key": "xyz"})


async def test_read_memory_finds_entry_by_keyword_in_quote(store) -> None:
    await _remember(store, "以后讲解都用中文", title="讲解语言")

    result = await ReadMemoryTool(store, body_max_bytes=4096).run({"query": "中文"})

    assert result["count"] == 1
    assert result["matches"][0]["key"]
    assert "以后讲解都用中文" in result["matches"][0]["content"]
    assert result["matches"][0]["truncated"] is False


async def test_read_memory_no_match(store) -> None:
    await _remember(store, "以后讲解都用中文")

    result = await ReadMemoryTool(store, body_max_bytes=4096).run({"query": "日语"})

    assert result["count"] == 0
    assert result["matches"] == []


async def test_read_memory_blank_query_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await ReadMemoryTool(store, body_max_bytes=4096).run({"query": ""})


async def test_read_memory_respects_body_budget(store) -> None:
    await _remember(store, "字" * 3000, title="长内容")

    result = await ReadMemoryTool(store, body_max_bytes=100).run({"query": "字"})

    match = result["matches"][0]
    assert len(match["content"].encode("utf-8")) <= 100
    assert match["truncated"] is True


# ---------- mistake 身份（P0：不同错题不得互相覆盖） ----------


async def test_two_different_mistakes_are_two_entries(store) -> None:
    """两条不同的错题必须各占一个文件，后者不得覆盖前者。"""
    first = await _remember(store, "I go to school yesterday", kind="mistake", title="过去式")
    second = await _remember(store, "He like play basketball", kind="mistake", title="三单")

    assert first["key"] != second["key"]
    assert len(store.list_entries()) == 2
    # 第一条还在，内容没被顶掉
    assert store.get(first["key"]).quote == "I go to school yesterday"
    assert store.get(second["key"]).quote == "He like play basketball"


async def test_same_mistake_twice_increments_count(store) -> None:
    """同一条错重复记仍算同一条（去重语义不变），只累加次数。"""
    first = await _remember(store, "I go to school yesterday", kind="mistake")
    second = await _remember(store, "I go to school yesterday", kind="mistake")

    assert second["key"] == first["key"]
    assert second["action"] == "updated"
    assert store.get(first["key"]).count == 2


async def test_mistake_identity_prefers_original_and_correction(store) -> None:
    """给了原文/改法就按它们算身份：讲解或标题不同，仍算同一条错。"""
    first = await _remember(
        store,
        "yesterday 要用过去式",
        kind="mistake",
        title="过去式",
        original="I go",
        correction="I went",
    )
    second = await _remember(
        store,
        "讲得详细一点：昨天发生的事，动词要变成过去式",
        kind="mistake",
        title="过去式（详版）",
        original="I go",
        correction="I went",
    )

    assert second["key"] == first["key"]
    assert store.get(first["key"]).count == 2
    # 原文/改法要落盘，read_memory 才检索得到
    assert store.get(first["key"]).original == "I go"
    assert store.get(first["key"]).correction == "I went"


async def test_mistake_with_different_originals_is_different_entries(store) -> None:
    """原文不同 → 就是两条不同的错，即使 title 一样。"""
    first = await _remember(
        store, "时态错", kind="mistake", title="时态", original="I go", correction="I went"
    )
    second = await _remember(
        store, "时态错", kind="mistake", title="时态", original="He like", correction="He likes"
    )

    assert first["key"] != second["key"]
    assert len(store.list_entries()) == 2


# ---------- T04：按 key 直接读取（目录已经指到某条，就别再靠关键词试探） ----------


async def test_read_memory_by_key_returns_body_in_one_call(store) -> None:
    """目录里已经有了 key，一次就该拿到正文，不用猜关键词。"""
    created = await _remember(store, "讲解都用中文", title="讲解语言")

    result = await ReadMemoryTool(store, body_max_bytes=4096).run({"key": created["key"]})

    assert result["count"] == 1
    assert result["matches"][0]["key"] == created["key"]
    assert "讲解都用中文" in result["matches"][0]["content"]


async def test_read_memory_missing_key_admits_it(store) -> None:
    """key 不存在：不能抛异常炸循环，也不能静默当成功 —— 要如实说没有。"""
    result = await ReadMemoryTool(store, body_max_bytes=4096).run({"key": "nosuchkey123"})

    assert result["count"] == 0
    assert result["matches"] == []
    # 得有一句人话，模型才知道该去问用户，而不是自己编
    assert "没有" in result.get("note", "")


async def test_read_memory_deleted_key_is_not_found(store) -> None:
    """软删（进 .trash/）之后按 key 读不到，与"没找到"行为一致。"""
    created = await _remember(store, "临时偏好", title="临时")
    await ForgetTool(store).run({"key": created["key"]})

    result = await ReadMemoryTool(store, body_max_bytes=4096).run({"key": created["key"]})

    assert result["count"] == 0


async def test_read_memory_neither_key_nor_query_is_tool_error(store) -> None:
    """两个都不给 = 调用方没说要找什么，属于用法错误（错误即数据，抛 ToolError）。"""
    from glyph.runtime.errors import ToolError

    with pytest.raises(ToolError):
        await ReadMemoryTool(store, body_max_bytes=4096).run({})


async def test_read_memory_query_still_works(store) -> None:
    """关键词查询是既有能力，新增 key 之后不能把它弄坏。"""
    await _remember(store, "以后讲解都用中文", title="讲解语言")

    result = await ReadMemoryTool(store, body_max_bytes=4096).run({"query": "中文"})

    assert result["count"] == 1
    assert result["matches"][0]["title"] == "讲解语言"


async def test_read_memory_by_key_truncates_within_budget(store) -> None:
    """正文超出预算要截断并标记，不能把整条塞爆上下文。"""
    created = await _remember(store, "长正文" * 200, title="很长")

    result = await ReadMemoryTool(store, body_max_bytes=200).run({"key": created["key"]})

    assert result["count"] == 1
    assert result["matches"][0]["truncated"] is True


# ---------- 记账工具 record_review_result（第二棒第④片）----------


def _enrolled_mistake(
    store,
    *,
    key="cafe000000aa",
    code="VERB:TENSE",
    stage="new",
    streak=0,
    due="2026-09-25",
    good=0,
    hard=0,
    wrong=0,
    last_review=None,
):
    """落一条已建档错题（fixture 时钟固定 2026-09-25，建档日 09-24、次日 09-25 首考）。"""
    store.upsert(
        MemoryEntry(
            key=key,
            type=MemoryType.MISTAKE,
            title="一般过去时",
            quote="I go home yesterday",
            count=1,
            first_seen="2026-09-24",
            last_seen="2026-09-24",
            last_mentioned_at="2026-09-24",
            tier="error",
            issue_type="hard",
            error_code=code,
            original="I go home yesterday",
            correction="I went home yesterday",
            mastery_stage=stage,
            independent_streak=streak,
            due_date=due,
            good_count=good,
            hard_count=hard,
            wrong_count=wrong,
            last_review_at=last_review,
            reopened_count=0,
        )
    )
    return key


async def _record(store, key, verdict, **extra):
    args = {"key": key, "verdict": verdict, "question": "题干 Q", "answer": "用户答案 A"}
    args.update(extra)
    return await RecordReviewResultTool(store).run(args)


async def test_record_independent_advances_schedules_and_appends_log(store) -> None:
    key = _enrolled_mistake(store)  # new、连对 0、09-25 首考

    result = await _record(store, key, "independent", explanation="过去时间状语要用过去式")

    assert result["verdict"] == "independent"
    assert result["stage"] == "learning"
    assert result["independent_streak"] == 1
    assert result["due_date"] == "2026-09-28"  # 第 1 次独立对 → 3 天后
    assert result["mastered"] is False
    # 落盘：账本推进，但 count 不累加
    got = store.get(key)
    assert got.mastery_stage == "learning"
    assert got.independent_streak == 1
    assert got.due_date == "2026-09-28"
    assert got.good_count == 1
    assert got.last_review_at == "2026-09-25"
    assert got.count == 1
    # 流水留痕一条，题干 / 答案 / 讲解原样
    reviews = store.list_reviews(key)
    assert len(reviews) == 1
    assert reviews[0]["date"] == "2026-09-25"
    assert reviews[0]["verdict"] == "independent"
    assert reviews[0]["question"] == "题干 Q"
    assert reviews[0]["answer"] == "用户答案 A"
    assert reviews[0]["explanation"] == "过去时间状语要用过去式"


async def test_record_third_independent_masters_and_clears_due(store) -> None:
    key = _enrolled_mistake(store, stage="learning", streak=2, good=2, due="2026-09-25")

    result = await _record(store, key, "independent")

    assert result["mastered"] is True
    assert result["stage"] == "mastered"
    assert result["independent_streak"] == 3
    assert result["due_date"] is None
    got = store.get(key)
    assert got.mastery_stage == "mastered"
    assert got.due_date is None


async def test_record_hinted_keeps_streak_and_reviews_next_day(store) -> None:
    key = _enrolled_mistake(store, stage="learning", streak=1, good=1, due="2026-09-25")

    result = await _record(store, key, "hinted")

    assert result["stage"] == "learning"
    assert result["independent_streak"] == 1  # 提示后对不升级、连对不增
    assert result["due_date"] == "2026-09-26"  # 次日再来
    assert store.get(key).hard_count == 1
    assert store.list_reviews(key)[0]["verdict"] == "hinted"


async def test_record_wrong_resets_streak_and_reviews_next_day(store) -> None:
    key = _enrolled_mistake(store, stage="learning", streak=2, good=2, due="2026-09-25")

    result = await _record(store, key, "wrong", explanation="正确答案：I went home")

    assert result["stage"] == "learning"
    assert result["independent_streak"] == 0  # 连对清零
    assert result["due_date"] == "2026-09-26"
    got = store.get(key)
    assert got.wrong_count == 1
    assert got.good_count == 2  # 历史独立对次数保留


async def test_record_bad_verdict_is_tool_error(store) -> None:
    key = _enrolled_mistake(store)
    with pytest.raises(ToolError):
        await _record(store, key, "almost")


async def test_record_bad_key_shape_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await _record(store, "xyz", "independent")


async def test_record_missing_key_is_tool_error(store) -> None:
    with pytest.raises(ToolError):
        await _record(store, "000000000000", "independent")


async def test_record_unenrolled_mistake_is_tool_error(store) -> None:
    """没进账本的普通错题不能记账（账无从记起）。"""
    await _remember(store, "I go home yesterday", kind="mistake", title="过去式")
    key = (await _remember(store, "I go home yesterday", kind="mistake"))["key"]

    with pytest.raises(ToolError):
        await _record(store, key, "independent")


async def test_record_mastered_point_is_tool_error(store) -> None:
    """已掌握的点不能直接记账：必须由"又犯错"触发复活，堵死绕过复活的路径。"""
    key = _enrolled_mistake(
        store, stage="mastered", streak=3, good=3, due=None, last_review="2026-09-20"
    )
    with pytest.raises(ToolError):
        await _record(store, key, "independent")


async def test_record_blank_question_or_answer_is_tool_error(store) -> None:
    key = _enrolled_mistake(store)
    with pytest.raises(ToolError):
        await RecordReviewResultTool(store).run(
            {"key": key, "verdict": "independent", "question": "  ", "answer": "a"}
        )
    with pytest.raises(ToolError):
        await RecordReviewResultTool(store).run(
            {"key": key, "verdict": "independent", "question": "q", "answer": ""}
        )


# ---------- remember 收 error_code：建账 / 学习中不动 / 掌握之后复活（⑤-5d）----------


async def test_remember_mistake_with_code_enrolls_new_ledger(store) -> None:
    result = await _remember(
        store,
        "yesterday I go home",
        kind="mistake",
        title="过去式",
        original="I go home yesterday",
        correction="I went home yesterday",
        error_code="VERB:TENSE",
    )

    mastery = result["mastery"]
    assert mastery["action"] == "enrolled"
    assert mastery["error_code"] == "VERB:TENSE"
    assert mastery["stage"] == "new"
    assert mastery["due_date"] == "2026-09-26"  # 建档 09-25，次日首考
    point = store.find_mastery_point_by_code("VERB:TENSE")
    assert point is not None
    assert point.key == result["key"]  # 本句成为代表条目
    assert point.stage is MasteryStage.NEW


async def test_remember_same_code_while_learning_adds_evidence_without_moving_ledger(
    store,
) -> None:
    # D18：学习中又犯同一知识点，只加证据，账本进度（连对/到期/计数）一律不动
    rep = _enrolled_mistake(store, stage="learning", streak=1, good=1, due="2026-09-28")

    result = await _remember(
        store,
        "last week he go there",
        kind="mistake",
        title="过去式2",
        original="He go there last week",
        correction="He went there last week",
        error_code="verb:tense",  # 小写也应规范化认下
    )

    assert result["mastery"]["action"] == "tracking"
    point = store.find_mastery_point_by_code("VERB:TENSE")
    assert point is not None
    assert point.key == rep  # 代表没换
    assert point.streak == 1
    assert point.good_count == 1
    assert point.due_date.isoformat() == "2026-09-28"  # 到期日没动
    # 新句子作为证据落盘：带 code、不背账本
    evidence = store.get(result["key"])
    assert evidence.error_code == "VERB:TENSE"
    assert evidence.mastery_stage is None


async def test_remember_same_code_after_mastered_reopens_ledger(store) -> None:
    # D10：掌握之后作文里又犯同一知识点 → 复活回 learning、连对清零、次日重走
    rep = _enrolled_mistake(
        store, stage="mastered", streak=3, good=3, due=None, last_review="2026-09-20"
    )

    result = await _remember(
        store,
        "yesterday I goes home",
        kind="mistake",
        title="过去式复发",
        original="I goes home yesterday",
        correction="I went home yesterday",
        error_code="VERB:TENSE",
    )

    assert result["mastery"]["action"] == "reopened"
    point = store.find_mastery_point_by_code("VERB:TENSE")
    assert point is not None
    assert point.key == rep  # 复活的是原来那本账
    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 0
    assert point.reopened_count == 1
    assert point.due_date.isoformat() == "2026-09-26"


async def test_remember_mistake_with_code_outside_whitelist_does_not_enroll(store) -> None:
    # D17：介词这类 6 类外硬错照常批改、可存错题，但不建账、code 也不落
    result = await _remember(
        store,
        "I go to home",
        kind="mistake",
        title="介词",
        original="I go to home",
        correction="I go home",
        error_code="PREP",
    )

    assert "mastery" not in result
    assert store.find_mastery_point_by_code("PREP") is None
    got = store.get(result["key"])
    assert got.mastery_stage is None
    assert got.error_code is None


async def test_remember_non_mistake_ignores_error_code(store) -> None:
    result = await _remember(
        store, "我喜欢每天早上读半小时英语", title="晨读习惯", error_code="VERB:TENSE"
    )

    assert "mastery" not in result
    assert store.find_mastery_point_by_code("VERB:TENSE") is None
