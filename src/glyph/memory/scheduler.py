"""提炼调度（D27）：什么时候该跑后台提炼，什么时候跳过。

纯逻辑、无 IO、时钟由调用方给——这样离线可测（R6）。

设计来源是 Claude Code 桌面端 2.1.295 的 extractMemories 调度：
- 两条触发线谁先到算谁：攒够 N 轮 / 空闲够久；
- 三条前置过滤先筛（没打字 / 已写过记忆 / 权限不许）；
- 提炼器在跑就不再开新的（"stashing for trailing run"）；
- 游标：跑完前移，**什么都没存下则不动**，下次重看这几轮。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = ["ExtractDecision", "TurnSample", "ExtractScheduler"]



class ExtractDecision(StrEnum):
    """一次调度判断的结论。跳过原因分开枚举，日志里能看出为什么没跑。"""

    EXTRACT = "extract"
    SKIP = "skip"  # 还不到时候（轮数不够、空闲不够）
    SKIP_NO_PROSE = "skip_no_prose"  # 这段里用户没打过字
    SKIP_ALREADY_WROTE = "skip_already_wrote"  # 主对话已直接写过记忆
    SKIP_NOT_ALLOWED = "skip_not_allowed"  # 权限 / 开关关掉了
    SKIP_IN_FLIGHT = "skip_in_flight"  # 上一次还没跑完



@dataclass(frozen=True)
class TurnSample:
    """一轮结束时的样本。字段刻意少：只放判定真正需要的。"""

    at: datetime
    has_user_prose: bool = True
    already_wrote_memory: bool = False



class ExtractScheduler:
    """提炼调度器。每次轮结束调 :meth:`observe`，按返回的结论决定跑不跑。

    不可重入检查（:attr:`_in_flight`）由调用方在真正开跑 / 跑完时置位，
    调度器自己不持有任务——它只回答"该不该跑"。
    """


    def __init__(
        self,
        *,
        every_turns: int,
        idle_seconds: int,
        has_user_prose: bool = True,
        already_wrote_memory: bool = False,
        allowed: bool = True,
    ) -> None:
        self._every_turns = max(1, every_turns)
        self._idle_seconds = max(0, idle_seconds)
        self._allowed = allowed
        # 这三项也支持一次性覆盖（会话级开关）；默认由构造参数定，observe 里可逐轮覆盖
        self._default_prose = has_user_prose
        self._default_wrote = already_wrote_memory
        self._turns = 0  # 攒了几轮
        self._last_turn_at: datetime | None = None
        self._in_flight = False
        self._cursor = 0  # 已处理到第几轮（跑完才前移）
        self._pending = 0  # 攒着还没处理的轮数

    # ---- 只读状态（测试与日志用）----


    def cursor(self) -> int:
        """已处理到的轮次游标。跑完前移；没存下东西就不动。"""
        return self._cursor


    def pending_turns(self) -> int:
        """攒着还没提炼的轮数。"""
        return self._pending


    def in_flight(self) -> bool:
        return self._in_flight


    # ---- 触发 ----


    def observe(self, sample: TurnSample) -> ExtractDecision:
        """一轮结束时调用。返回该不该提炼。"""
        if not self._allowed:
            return ExtractDecision.SKIP_NOT_ALLOWED
        if self._in_flight:
            return ExtractDecision.SKIP_IN_FLIGHT
        prose = sample.has_user_prose if sample.has_user_prose is not None else True
        if not prose:
            # 没打字 → 没有可提炼的东西；但时间戳仍要更新，避免下一轮误判空闲
            self._last_turn_at = sample.at
            return ExtractDecision.SKIP_NO_PROSE
        if sample.already_wrote_memory:
            self._last_turn_at = sample.at
            return ExtractDecision.SKIP_ALREADY_WROTE

        idle = self._idle_elapsed(sample.at)
        self._turns += 1
        self._pending += 1
        self._last_turn_at = sample.at

        # 两条触发线谁先到算谁
        if self._turns >= self._every_turns:
            return self._fire()
        if idle is not None and idle >= self._idle_seconds and self._idle_seconds > 0:
            return self._fire()
        return ExtractDecision.SKIP


    def flush_on_session_switch(self) -> ExtractDecision:
        """用户新建 / 切换会话：把上一段立刻收尾（D27 兜底）。"""
        if not self._allowed:
            return ExtractDecision.SKIP_NOT_ALLOWED
        if self._in_flight:
            return ExtractDecision.SKIP_IN_FLIGHT
        if self._pending <= 0:
            return ExtractDecision.SKIP
        return self._fire()


    def on_grading_finalized(self) -> ExtractDecision:
        """批改定稿：信息最全（错句、改法、错误类型都在手上），立刻提炼。"""
        if not self._allowed:
            return ExtractDecision.SKIP_NOT_ALLOWED
        if self._in_flight:
            return ExtractDecision.SKIP_IN_FLIGHT
        return self._fire()


    # ---- 收尾 ----


    def finish(self, *, saved: bool) -> None:
        """提炼跑完。``saved`` 表示这次有没有真写进暂存。

        抄 CC 的游标策略：跑完就前移；**什么都没存下则不动**——
        因为"没记下来"可能意味着"它没看明白"，下次还要再看一遍。
        """
        self._in_flight = False
        if saved:
            self._cursor += self._pending
            self._pending = 0
        # 没存下时 _pending 保持原样，下次重看

    def reset_for_new_session(self) -> None:
        """换会话：轮数、游标、待处理全部归零。"""
        self._turns = 0
        self._pending = 0
        self._cursor = 0
        self._last_turn_at = None
        self._in_flight = False

    # ---- 内部 ----


    def _idle_elapsed(self, now: datetime) -> float | None:
        if self._last_turn_at is None:
            return None
        return (now - self._last_turn_at).total_seconds()


    def _fire(self) -> ExtractDecision:
        self._turns = 0
        self._in_flight = True
        return ExtractDecision.EXTRACT
