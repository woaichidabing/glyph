"""提炼调度测试（D27）：什么时候跑、什么时候跳过。

全部离线、时钟可注入——R6 要求默认测试不联网、不依赖真实时间。
"""

from datetime import datetime, timedelta, timezone

from glyph.memory.scheduler import ExtractDecision, ExtractScheduler, TurnSample

_TZ = timezone(timedelta(hours=8))


def at(hour: int, minute: int = 0, *, day: int = 15) -> datetime:
    return datetime(2026, 10, day, hour, minute, 0, tzinfo=_TZ)



def make_scheduler(**overrides) -> ExtractScheduler:
    kwargs = dict(
        every_turns=5,
        idle_seconds=300,
        has_user_prose=True,
        already_wrote_memory=False,
        allowed=True,
    )
    kwargs.update(overrides)
    return ExtractScheduler(**kwargs)



def sample(now: datetime, *, prose: bool = True, wrote: bool = False) -> TurnSample:
    """一轮结束的样本：时间 + 本轮有没有用户文字 / 有没有直接写过记忆。"""
    return TurnSample(at=now, has_user_prose=prose, already_wrote_memory=wrote)



# ---- 轮数触发 ----


def test_fires_when_turn_threshold_reached() -> None:
    s = make_scheduler(every_turns=5)
    for i in range(4):
        d = s.observe(sample(at(10, i)))
        assert d == ExtractDecision.SKIP, f"第 {i + 1} 轮不该触发"
    d = s.observe(sample(at(10, 4)))
    assert d == ExtractDecision.EXTRACT



def test_counter_resets_after_firing() -> None:
    s = make_scheduler(every_turns=2)
    s.observe(sample(at(10, 0)))
    assert s.observe(sample(at(10, 1))) == ExtractDecision.EXTRACT
    s.finish(saved=True)  # 跑完才释放；不释放的话下一次是 SKIP_IN_FLIGHT
    # 重新数：还没攒够，不该马上又跑
    assert s.observe(sample(at(10, 2))) == ExtractDecision.SKIP
    assert s.observe(sample(at(10, 3))) == ExtractDecision.EXTRACT



# ---- 空闲触发（D27：CC 是 120 秒，Glyph 放宽到 300）----


def test_fires_on_idle_timeout_even_below_turn_threshold() -> None:
    s = make_scheduler(every_turns=5, idle_seconds=300)
    s.observe(sample(at(10, 0)))
    # 才 1 轮、不到 5 轮，但已经空闲 5 分钟以上 → 照样触发
    d = s.observe(sample(at(10, 6)))
    assert d == ExtractDecision.EXTRACT



def test_idle_below_threshold_does_not_fire() -> None:
    s = make_scheduler(every_turns=99, idle_seconds=300)
    s.observe(sample(at(10, 0)))
    assert s.observe(sample(at(10, 4))) == ExtractDecision.SKIP



# ---- 三条前置过滤（抄 CC）----


def test_skip_when_no_user_prose() -> None:
    """用户只传图 / 点按钮、没打字 → 没有可提炼的东西，不花钱。"""
    s = make_scheduler(every_turns=1)
    assert s.observe(sample(at(10, 0), prose=False)) == ExtractDecision.SKIP_NO_PROSE



def test_skip_when_conversation_already_wrote_memory() -> None:
    """主对话已经直接写过（用户明说"记住"）→ 不重复提炼。"""
    s = make_scheduler(every_turns=1)
    d = s.observe(sample(at(10, 0), wrote=True))
    assert d == ExtractDecision.SKIP_ALREADY_WROTE



def test_skip_when_not_allowed() -> None:
    """权限 / 用户开关关掉了 → 不跑。"""
    s = make_scheduler(every_turns=1, allowed=False)
    assert s.observe(sample(at(10, 0))) == ExtractDecision.SKIP_NOT_ALLOWED



def test_prose_filter_beats_turn_threshold() -> None:
    """过滤先于触发判断：轮数够了但没打字，照样跳过。"""
    s = make_scheduler(every_turns=1, idle_seconds=0)
    assert s.observe(sample(at(10, 0), prose=False)) == ExtractDecision.SKIP_NO_PROSE



# ---- 会话切换：立刻把上一段收尾（D27 兜底）----


def test_fires_immediately_on_session_switch() -> None:
    s = make_scheduler(every_turns=99, idle_seconds=99999)
    s.observe(sample(at(10, 0)))
    assert s.flush_on_session_switch() == ExtractDecision.EXTRACT



def test_session_switch_flush_is_noop_without_pending() -> None:
    s = make_scheduler()
    assert s.flush_on_session_switch() == ExtractDecision.SKIP



def test_session_switch_flush_is_noop_when_not_allowed() -> None:
    """不允许时如实报"不允许"，不报含糊的 SKIP——日志要能看出为什么没跑。"""
    s = make_scheduler(allowed=False)
    s.observe(sample(at(10, 0)))
    assert s.flush_on_session_switch() == ExtractDecision.SKIP_NOT_ALLOWED



# ---- 批改定稿：立刻提炼（信息最全）----


def test_fires_immediately_after_grading_finalize() -> None:
    s = make_scheduler(every_turns=99, idle_seconds=99999)
    assert s.on_grading_finalized() == ExtractDecision.EXTRACT



def test_grading_finalize_respects_filters() -> None:
    s = make_scheduler(allowed=False)
    assert s.on_grading_finalized() == ExtractDecision.SKIP_NOT_ALLOWED



# ---- 防重复：提炼器在跑就不再开新的 ----


def test_no_new_run_while_extraction_in_flight() -> None:
    """抄 CC：extraction in progress → 暂存，不并发。"""
    s = make_scheduler(every_turns=1)
    assert s.observe(sample(at(10, 0))) == ExtractDecision.EXTRACT
    assert s.observe(sample(at(10, 1))) == ExtractDecision.SKIP_IN_FLIGHT



def test_finish_releases_in_flight() -> None:
    s = make_scheduler(every_turns=1)
    s.observe(sample(at(10, 0)))
    s.finish(saved=True)
    assert s.observe(sample(at(10, 1))) == ExtractDecision.EXTRACT



# ---- 游标：跑完前移，没产出不动（D27 第⑤条）----


def test_cursor_advances_only_when_something_was_saved() -> None:
    s = make_scheduler(every_turns=1)
    s.observe(sample(at(10, 0)))
    assert s.cursor() == 0
    s.finish(saved=True)
    assert s.cursor() == 1



def test_cursor_stays_when_nothing_saved() -> None:
    """什么都没存下 → 不动，下次重看这几轮（CC 原文：the cursor stays）。"""
    s = make_scheduler(every_turns=1)
    s.observe(sample(at(10, 0)))
    s.finish(saved=False)
    assert s.cursor() == 0



def test_cursor_reset_on_session_switch() -> None:
    s = make_scheduler(every_turns=1)
    s.observe(sample(at(10, 0)))
    s.finish(saved=True)
    s.reset_for_new_session()
    assert s.cursor() == 0
    assert s.pending_turns() == 0



# ---- 与整理层的分界：调度器只管提炼 ----


def test_cursor_advances_by_pending_count() -> None:
    """一次提炼处理了 3 轮，游标一次前进 3。"""
    s = make_scheduler(every_turns=3)
    for i in range(3):
        s.observe(sample(at(10, i)))
    s.finish(saved=True)
    assert s.cursor() == 3
    assert s.pending_turns() == 0


def test_decision_enum_is_exhaustive_and_stable() -> None:
    """枚举值稳定，前端 / 日志按它分支，不许随手改名。"""
    expected = {
        "EXTRACT", "SKIP", "SKIP_NO_PROSE", "SKIP_ALREADY_WROTE",
        "SKIP_NOT_ALLOWED", "SKIP_IN_FLIGHT",
    }
    assert {d.name for d in ExtractDecision} == expected
