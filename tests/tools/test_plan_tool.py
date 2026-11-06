"""propose_plan 工具：把"帮用户规划"的结果结构化返回。

工具本身**不落库**（计划持久化在桌面端 localStorage，由用户在计划卡上点
"加入我的计划"后写入，src=markr）；它的职责是约束主脑按 {goal, tasks[]}
产出、并做基本校验，避免计划散在一大段自然语言里没法一键收纳。
"""

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.plan_tools import ProposePlanTool


async def test_propose_plan_returns_goal_and_task_texts() -> None:
    out = await ProposePlanTool().run({
        "goal": "三个月内通过四级",
        "tasks": [{"t": "每天背 30 个四级词"}, {"t": "每周写一篇作文并批改"}],
    })
    assert out["proposed"] is True
    assert out["goal"] == "三个月内通过四级"
    assert [item["t"] for item in out["tasks"]] == ["每天背 30 个四级词", "每周写一篇作文并批改"]


async def test_blank_task_items_are_trimmed() -> None:
    out = await ProposePlanTool().run({
        "goal": "练好口语",
        "tasks": [{"t": "  每天跟读 10 分钟  "}, {"t": "   "}, {"t": ""}],
    })
    assert [item["t"] for item in out["tasks"]] == ["每天跟读 10 分钟"]


async def test_empty_goal_is_an_error() -> None:
    with pytest.raises(ToolError):
        await ProposePlanTool().run({"goal": "  ", "tasks": [{"t": "做一件事"}]})


async def test_no_tasks_is_an_error() -> None:
    with pytest.raises(ToolError):
        await ProposePlanTool().run({"goal": "过四级", "tasks": []})


async def test_all_blank_tasks_is_an_error() -> None:
    with pytest.raises(ToolError):
        await ProposePlanTool().run({"goal": "过四级", "tasks": [{"t": "  "}]})


async def test_too_many_tasks_is_an_error() -> None:
    many = [{"t": f"第 {i} 件事"} for i in range(ProposePlanTool.MAX_TASKS + 1)]
    with pytest.raises(ToolError):
        await ProposePlanTool().run({"goal": "一个大目标", "tasks": many})
