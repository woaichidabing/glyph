"""计划路由：目标、任务、自动完成与旧数据迁移。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from .. import runtime as rout
from ..deps import state

router = APIRouter()

__all__ = [
    "PlanEventIn",
    "PlanGoalIn",
    "PlanPhasesIn",
    "PlanRhythmIn",
    "PlanImportIn",
    "PlanTaskIn",
    "PlanTasksIn",
    "PlanToggleIn",
    "add_plan_tasks",
    "delete_plan_task",
    "get_plan",
    "import_plan",
    "plan_event",
    "set_plan_goal",
    "toggle_plan_task",
]


class PlanGoalIn(rout.BaseModel):
    text: str
    weeks: int = 12
    start_date: str | None = None


class PlanTaskIn(rout.BaseModel):
    t: str
    # 来源：own=用户自己加的；glyph=计划里排的。不传就按 glyph
    src: str | None = None
    kind: str | None = None
    target: int = 1
    scheduledFor: str | None = None
    id: str | None = None
    progress: int = 0
    done: bool = False
    doneAt: int | None = None
    evidence: str | None = None


class PlanTasksIn(rout.BaseModel):
    tasks: list[PlanTaskIn]
    scheduled_for: str | None = None


class PlanToggleIn(rout.BaseModel):
    done: bool | None = None


class PlanPhasesIn(rout.BaseModel):
    phases: list[dict[str, rout.Any]] = []


class PlanRhythmIn(rout.BaseModel):
    """周节奏：键 "1".."7"（周一=1），值是任务文案或 {t, kind}。"""

    rhythm: dict[str, list[dict[str, rout.Any]]] = {}
    # True = 改模板时把本周剩下的日子按新节奏重建（已完成的保留）
    sync: bool = False


class PlanImportIn(rout.BaseModel):
    goal: dict[str, rout.Any] | None = None
    tasks: list[dict[str, rout.Any]] = []


class PlanEventIn(rout.BaseModel):
    kind: str
    evidence: str | None = None
    amount: int = 1


def _bad_request(exc: ValueError) -> rout.HTTPException:
    return rout.HTTPException(status_code=400, detail=str(exc) or "计划参数不合法")


@router.get("/api/plan")
async def get_plan() -> dict:
    return state.plan_store.snapshot()


@router.post("/api/plan/goal")
async def set_plan_goal(req: PlanGoalIn) -> dict:
    try:
        return state.plan_store.set_goal(req.text, weeks=req.weeks, start_date=req.start_date)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/api/plan/reset")
async def reset_plan() -> dict:
    return state.plan_store.reset()


@router.put("/api/plan/phases")
async def set_plan_phases(req: PlanPhasesIn) -> dict:
    try:
        return state.plan_store.set_phases(req.phases)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.put("/api/plan/rhythm")
async def set_plan_rhythm(req: PlanRhythmIn) -> dict:
    try:
        return state.plan_store.set_rhythm(req.rhythm, sync=req.sync)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/api/plan/tasks")
async def add_plan_tasks(req: PlanTasksIn) -> dict:
    try:
        tasks = [item.model_dump(exclude_none=True) for item in req.tasks]
        return state.plan_store.add_tasks(tasks, scheduled_for=req.scheduled_for)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.patch("/api/plan/tasks/{task_id}")
async def toggle_plan_task(task_id: str, req: PlanToggleIn) -> dict:
    try:
        return state.plan_store.toggle_task(task_id, done=req.done)
    except KeyError as exc:
        raise rout.HTTPException(status_code=404, detail="计划任务不存在") from exc


@router.delete("/api/plan/tasks/{task_id}")
async def delete_plan_task(task_id: str) -> dict:
    try:
        return state.plan_store.delete_task(task_id)
    except KeyError as exc:
        raise rout.HTTPException(status_code=404, detail="计划任务不存在") from exc


class PlanDayIn(rout.BaseModel):
    """换掉某一天的任务；scope=weekly 时同时写回周节奏。"""

    date: str
    items: list[dict[str, Any]] = []
    scope: Literal["day", "weekly"] = "day"


@router.post("/api/plan/day")
def set_plan_day(req: PlanDayIn) -> dict:
    try:
        snap = state.plan_store.replace_day(req.date, req.items, scope=req.scope)
    except Exception as exc:  # noqa: BLE001 - 统一转成人话
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snap


@router.post("/api/plan/event")
async def plan_event(req: PlanEventIn) -> dict:
    try:
        return state.plan_store.complete_kind(
            req.kind, evidence=req.evidence, amount=req.amount
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/api/plan/import")
async def import_plan(req: PlanImportIn) -> dict:
    try:
        return state.plan_store.import_state(req.goal, req.tasks)
    except ValueError as exc:
        raise _bad_request(exc) from exc
