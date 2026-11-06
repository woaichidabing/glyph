"""计划接口：目标 / 任务 / 勾选 / 自动完成 / 迁移。"""

from __future__ import annotations

from pathlib import Path

import pytest

from glyph.plan import PlanStore
from web.deps import state as web_state
from web.routers import plan as plan_api


@pytest.fixture()
def plan_env(monkeypatch, tmp_path: Path) -> PlanStore:
    store = PlanStore(tmp_path, clock=lambda: 1_760_000_000_000, today=lambda: "2026-10-21")
    monkeypatch.setattr(web_state, "plan_store", store)
    return store


@pytest.mark.asyncio
async def test_plan_empty_and_goal(plan_env: PlanStore) -> None:
    assert await plan_api.get_plan() == {
        "version": 2,
        "goal": None,
        "phases": [],
        "rhythm": {},
        "tasks": [],
    }
    snap = await plan_api.set_plan_goal(plan_api.PlanGoalIn(text="三个月过六级"))
    assert snap["goal"]["text"] == "三个月过六级"


@pytest.mark.asyncio
async def test_plan_add_toggle_delete(plan_env: PlanStore) -> None:
    snap = await plan_api.add_plan_tasks(
        plan_api.PlanTasksIn(tasks=[plan_api.PlanTaskIn(t="背 20 个词")])
    )
    task = snap["tasks"][0]
    assert task["kind"] == "study"
    toggled = await plan_api.toggle_plan_task(
        task["id"], plan_api.PlanToggleIn(done=True)
    )
    assert toggled["task"]["done"] is True
    deleted = await plan_api.delete_plan_task(task["id"])
    assert deleted["removed"]["id"] == task["id"]
    assert deleted["tasks"] == []


@pytest.mark.asyncio
async def test_plan_event_completes_dict_task(plan_env: PlanStore) -> None:
    await plan_api.add_plan_tasks(
        plan_api.PlanTasksIn(
            tasks=[plan_api.PlanTaskIn(t="查 1 个生词", kind="dict", target=1)]
        )
    )
    result = await plan_api.plan_event(
        plan_api.PlanEventIn(kind="dict", evidence="查过 commit")
    )
    assert result["task"]["done"] is True
    assert result["task"]["evidence"] == "查过 commit"


@pytest.mark.asyncio
async def test_plan_import_is_idempotent(plan_env: PlanStore) -> None:
    payload = plan_api.PlanImportIn(
        goal={"text": "旧目标", "weeks": 12, "startDate": "2026-10-20"},
        tasks=[{"id": "legacy-1", "t": "自己加一件", "src": "own", "done": False}],
    )
    first = await plan_api.import_plan(payload)
    second = await plan_api.import_plan(payload)
    assert first["importedTasks"] == 1
    assert second["importedTasks"] == 0
    assert first["goal"]["text"] == "旧目标"


@pytest.mark.asyncio
async def test_plan_rhythm_and_phases(plan_env: PlanStore) -> None:
    """周节奏写进去后，今天（周三）的接口应自动带出今天的任务。"""
    snap = await plan_api.set_plan_rhythm(
        plan_api.PlanRhythmIn(rhythm={"3": [{"t": "背词 30 分钟", "kind": "study"}]})
    )
    assert [x["t"] for x in snap["tasks"]] == ["背词 30 分钟"]
    phases = await plan_api.set_plan_phases(
        plan_api.PlanPhasesIn(phases=[{"fromWeek": 1, "toWeek": 4, "title": "打底"}])
    )
    assert phases["phases"][0]["title"] == "打底"


@pytest.mark.asyncio
async def test_plan_reset(plan_env: PlanStore) -> None:
    await plan_api.set_plan_goal(plan_api.PlanGoalIn(text="旧目标"))
    await plan_api.add_plan_tasks(plan_api.PlanTasksIn(tasks=[plan_api.PlanTaskIn(t="旧任务")]))
    snap = await plan_api.reset_plan()
    assert snap["goal"] is None and snap["tasks"] == []


@pytest.mark.asyncio
async def test_plan_task_src_kept(plan_env: PlanStore) -> None:
    """用户自己加的任务，来源要存成 own（以前被后端丢掉，界面显示成 Glyph 排的）。"""
    snap = await plan_api.add_plan_tasks(
        plan_api.PlanTasksIn(tasks=[plan_api.PlanTaskIn(t="自己加的", src="own")])
    )
    assert snap["tasks"][0]["src"] == "own"
