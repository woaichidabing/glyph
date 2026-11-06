"""read_plan：让对话读到当前计划目标与今天的任务。"""

from __future__ import annotations

from typing import Any

from datetime import date

from ..plan import PlanStore
from ..runtime.errors import ToolError
from .base import ReadOnlyTool

__all__ = ["ReadPlanTool"]


def _week_number(goal: dict[str, Any], today: str) -> int:
    """第几周（1 起）；算不出来就当第 1 周。"""
    try:
        start = date.fromisoformat(goal.get("startDate") or today)
        now = date.fromisoformat(today)
        return max(1, (now - start).days // 7 + 1)
    except (TypeError, ValueError):
        return 1


class ReadPlanTool(ReadOnlyTool):
    name = "read_plan"

    description = (
        "当用户问“我今天要做什么”“我的计划到哪了”“我这周怎么安排”时用。\n"
        "返回：长期目标、当前阶段（第几周到第几周练什么）、整周节奏（周一到周日各练什么）、"
        "今天的任务（带完成状态 / 来源 / 证据）与完成数。\n"
        "只读，不修改计划；要改仍走 propose_plan + 界面确认。\n"
        "用户自己在页面上改过节奏也以这里返回的为准，不要拿旧印象覆盖。"
    )

    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, store: PlanStore) -> None:
        self._store = store

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        try:
            snapshot = self._store.snapshot()
        except Exception as exc:  # noqa: BLE001 - 工具错误即数据，不炸循环
            raise ToolError(f"计划读不出来：{exc}") from exc
        today = self._store.today
        tasks = [
            t
            for t in snapshot.get("tasks", [])
            if t.get("scheduledFor") in (None, today)
            and (t.get("scheduledFor") == today or t.get("createdAt") == today)
        ]
        tasks = tasks[:20]
        done = sum(1 for t in tasks if t.get("done"))
        goal = snapshot.get("goal")
        phases = snapshot.get("phases") or []
        rhythm = snapshot.get("rhythm") or {}
        week_no = _week_number(goal, today) if goal else None
        current = next(
            (p for p in phases if week_no and p["fromWeek"] <= week_no <= p["toWeek"]),
            None,
        )
        weekday = str(date.fromisoformat(today).isoweekday())
        return {
            "goal": goal,
            "week": week_no,
            "phase": current,
            "phases": phases,
            "rhythm": rhythm,
            "todayRhythm": rhythm.get(weekday) or [],
            "today": tasks,
            "todayDone": done,
            "todayTotal": len(tasks),
            "note": (
                "任务由周节奏每天自动展开；没做完的会留在计划页，不会被清掉。"
                "用户在页面上改过节奏时，以这里返回的 rhythm 为准。"
            ),
        }
