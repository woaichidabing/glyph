"""read_plan 工具：只读今天计划与目标。"""

from __future__ import annotations

from pathlib import Path

import pytest

from glyph.plan import PlanStore
from glyph.tools.plan_read import ReadPlanTool


@pytest.mark.asyncio
async def test_read_plan_returns_goal_and_today(tmp_path: Path) -> None:
    store = PlanStore(tmp_path, clock=lambda: 1_760_000_000_000, today=lambda: "2026-10-21")
    store.set_goal("三个月过六级")
    store.add_tasks(
        [
            {"t": "背 20 个词", "kind": "study"},
            {"t": "查 3 个生词", "kind": "dict", "target": 3},
        ]
    )
    result = await ReadPlanTool(store).run({})
    assert result["goal"]["text"] == "三个月过六级"
    assert result["todayTotal"] == 2
    assert result["todayDone"] == 0
    assert result["today"][0]["kind"] == "study"


@pytest.mark.asyncio
async def test_read_plan_does_not_include_future_tasks(tmp_path: Path) -> None:
    store = PlanStore(tmp_path, clock=lambda: 1_760_000_000_000, today=lambda: "2026-10-21")
    store.add_tasks([{"t": "背 20 个词", "scheduledFor": "2026-10-22"}])
    result = await ReadPlanTool(store).run({})
    assert result["today"] == []
