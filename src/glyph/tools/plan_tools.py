"""让 Glyph 规划：把用户的目标拆成一份可执行的学习计划（结构化返回）。

- propose_plan（**只读**）：用户说出一个目标 / 想法（"想三个月过四级""想把邮件
  写地道"），主脑给出一句话目标 goal + 一组具体小动作 tasks，结构化返回。
  工具**不写任何存储**——计划持久化在桌面端本地（localStorage），界面会把这份
  计划做成一张卡，用户点"加入我的计划"后才真正落进计划清单（来源标记 markr）。
  所以工具不能假装计划已经建好，落库这个动作只属于用户的确认。

R5「错误即数据」：目标为空 / 没有有效任务 / 任务条数超上限，属于参数违规，
抛 ToolError 让主脑补正，而不是静默返回一份空计划。
"""

from __future__ import annotations

from typing import Any

from ..runtime.errors import ToolError
from .base import ReadOnlyTool

__all__ = ["ProposePlanTool"]


class ProposePlanTool(ReadOnlyTool):
    """把一个学习目标拆成结构化计划，交界面确认后落库。"""

    name = "propose_plan"

    # 一份起步计划最多塞多少条：太多等于没重点，用户也不会做。
    MAX_TASKS = 12

    description = (
        "当用户说出一个**目标 / 想法、想要个安排或计划**时用（如"
        "“我想三个月过四级”“想把工作邮件写地道”“帮我规划下怎么背六级词”）。\n"
        "- goal：一句话说清他想达成什么；\n"
        "- tasks：3 到 8 条**具体、近期就能动手的小动作**，每条一句话、"
        "说清做什么、大概做多少（例如“每天跟读 10 分钟”而不是“提高听力”这种空话）；\n"
        "- phases（可选）：把整个周期切成 2–4 个阶段（第几周到第几周、这段重点练什么）；\n"
        "- rhythm（可选）：一周七天各练什么（周一=1）。**给了 rhythm，计划页就会每天"
        "按它自动长出当天任务**，不会出现“昨天做完今天空了”。\n"
        "  ⚠️ 给了 rhythm 时，**别再把这些动作重复塞进 tasks**："
        "tasks 只放节奏之外的里程碑/检查点（如“第 4 周做一次模考”），"
        "否则用户会在今天的清单里看到“每周做 10 篇阅读”这种本不该今天做的事。\n"
        "**不要排具体日期 / 不做逾期**，也不要把一天排满；学习任务是“今天的事”，漏了还在清单里。\n"
        "工具只负责把计划结构化交出来，界面会让用户**确认后**才加入计划——"
        "在他确认前，不要在回复里谎称计划已经建好了。\n"
        "缺关键信息时（只说了每天多少分钟、没说练什么；或只说了目标、没说时间）"
        "先用 ask_user 弹卡片问清，别自己替他选方向，也别在正文里问。\n"
        "不要用于：单道题、单次批改 / 润色 / 翻译、查词——那些走对应工具，不必做计划。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "一句话目标，说清想达成什么。",
            },
            "phases": {
                "type": "array",
                "description": (
                    "可选：把整个周期分成 2 到 4 个阶段，每段 1–4 周。"
                    "每项 {fromWeek, toWeek, title, focus}。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "fromWeek": {"type": "integer", "description": "起始周（1 起）"},
                        "toWeek": {"type": "integer", "description": "结束周（含）"},
                        "title": {"type": "string", "description": "阶段名，如“打底”"},
                        "focus": {"type": "string", "description": "一句话说这段重点练什么"},
                    },
                    "required": ["fromWeek", "toWeek", "title"],
                    "additionalProperties": False,
                },
            },
            "rhythm": {
                "type": "object",
                "description": (
                    "可选：一周七天的固定节奏，键 1 到 7（周一=1），"
                    "值是任务数组 [{\"t\": \"背词 30 分钟\"}]。空的哪天就是休息。"
                ),
                "additionalProperties": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"t": {"type": "string"}},
                        "required": ["t"],
                        "additionalProperties": False,
                    },
                },
            },
            "tasks": {
                "type": "array",
                "description": (
                    "3 到 8 条具体可执行的小动作；每条 {\"t\": \"一句话任务\"}。"
                    "若同时给了 rhythm，这里只放节奏装不下的里程碑，不要重复日常动作。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "t": {"type": "string", "description": "一句话任务，含动作和大概量。"},
                    },
                    "required": ["t"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["goal", "tasks"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        goal = str(args.get("goal", "")).strip()
        if not goal:
            raise ToolError("目标 goal 是空的。用一句话说清用户想达成什么，再调一次。")

        raw = args.get("tasks") or []
        tasks: list[dict[str, str]] = []
        for item in raw:
            # 容忍模型把任务直接写成字符串，或包成 {"t": ...}；空白项丢弃
            text = item.get("t", "") if isinstance(item, dict) else item
            if isinstance(text, str):
                clean = text.strip()
                if clean:
                    tasks.append({"t": clean})

        if not tasks:
            raise ToolError(
                "没有给出有效任务 tasks。给 3 到 8 条具体、近期能动手的小动作，"
                "每条一句话说清做什么、做多少，再调一次。"
            )
        if len(tasks) > self.MAX_TASKS:
            raise ToolError(
                f"一次给了 {len(tasks)} 条任务，太多等于没重点。请精简到不超过 "
                f"{self.MAX_TASKS} 条、优先最该先做的几件事，再调一次。"
            )

        phases = args.get("phases") or []
        rhythm = args.get("rhythm") or {}

        return {
            "proposed": True,
            "goal": goal,
            "tasks": tasks,
            "phases": phases if isinstance(phases, list) else [],
            "rhythm": rhythm if isinstance(rhythm, dict) else {},
            "note": (
                "计划已按结构给出。界面会让用户确认，他点“加入我的计划”后这些任务才会"
                "落进计划清单（来源 markr）；确认前不要声称计划已经建好。"
            ),
        }
