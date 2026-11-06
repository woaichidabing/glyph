"""ask_user：拿不准时停下来问用户一句（2–4 个选项，用户也可以自己说）。

为什么它是"工具"而不是主循环里的特殊机制（铁律 R2「工具是唯一能力入口」）：
模型什么时候该问、问什么、给哪几个选项，都是**模型的判断**，跟批改、查词一样该由它
自己决定；运行时只负责"把问题递到界面上、把答案带回来"。做成工具还能白捡三套既有
机制——错误即数据（R5）、取消收得住、答案照常进会话账本。

模型侧契约（对齐 Claude Code / Codex / WorkBuddy 三家共识，见
``.workbuddy/memory/2026-10-08.md``）：

- **选项 2–4 个**，推荐项放第一个并在标签后加 ``(Recommended)``；
- **"我自己说"由界面自动补**，模型不许自己造一个"其他"选项；
- **违规不报错，回一段"纠偏文本"**（抄 Claude Code 的 validationErrorSteer）：
  抛异常会让模型以为这工具坏了、转头换别的招；回一段人话它才知道该怎么改，
  而且用户全程无感（那个没人该看到的问题根本没发出去）；
- **答案回灌是一句人话、分三档语气**：选了选项 / 自己打了字 / 没等来回答。
  不用裸 JSON——我们的主脑是轻量模型，读句子比解析结构稳；而且"用户自己打字"
  那一档必须提醒模型"他可能是要你改，照他说的办"。

**边界（刻意不做的）**：
- 不检查"选项跟问题相不相关"——三家都没做，语义校验做不准还烧 token，
  兜底成本极低（用户永远能自己打字）；
- 不做无人值守自动放行；没等到回答就如实说没等到，交回模型自己判断。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from .base import ReadOnlyTool

__all__ = ["AskUserAnswer", "AskUserGateway", "AskUserTool"]

MAX_OPTIONS = 4
MIN_OPTIONS = 2


@dataclass(frozen=True)
class AskUserAnswer:
    """用户的一次答复。

    ``kind`` 三选一，决定回灌给模型的语气：
    - ``choice``：点了某个选项（``choice`` 是那个选项的标签）；
    - ``custom``：没点选项、自己打了字（``text`` 是原话）；
    - ``abandoned``：没等来回答（关窗口 / 用户没理），**绝不当成"用户选了什么"**。
    """

    choice: str | None = None
    text: str = ""
    kind: Literal["choice", "custom", "abandoned"] = "choice"


class AskUserGateway:
    """界面 ↔ 挂起中的工具之间那唯一一条"回答通道"。

    一次只允许**一个**待回答的问题：界面上同时弹两张卡片会让用户懵，所以第二个
    问题会被工具直接挡回去（见 :meth:`AskUserTool.run`）。

    它是"通道"而非"状态"：不认识会话、不认识 HTTP，只负责把答案交到等待中的那个
    协程手上。谁把答案送进来（web 接口 / 桌面 sidecar / 测试）由装配层决定。
    """

    def __init__(self) -> None:
        self._pending: asyncio.Future[AskUserAnswer] | None = None
        self._question: str = ""

    @property
    def has_pending(self) -> bool:
        """是否有一个正在等回答的问题。"""
        return self._pending is not None and not self._pending.done()

    @property
    def pending_question(self) -> str:
        """当前挂着等回答的那句话（没有就返回空串）。"""
        return self._question if self.has_pending else ""

    async def ask(self, *, question: str, options: list[dict[str, str]]) -> AskUserAnswer:
        """挂起等待用户答复；取消（CancelledError）原样向外传，由主循环收口。"""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AskUserAnswer] = loop.create_future()
        self._pending = future
        self._question = question
        try:
            return await future
        finally:
            self._pending = None
            self._question = ""

    def answer(
        self,
        *,
        choice: str | None = None,
        text: str = "",
        question: str | None = None,
    ) -> bool:
        """用户点了选项或自己打了字。没有待回答的问题 / 对不上就返回 False。

        自由输入优先：只要用户打了字，就按"他自己说明的"处理（他很可能是在改需求，
        不是在选择）。空答复不接受——空串没有任何信息量。

        ``question`` 是可选的**对账用的问题原文**：调用方把他答复的那张卡片上的问题
        带回来，与当前挂着的那句对不上就拒绝。防的是"上一轮卡片的迟到点击"把答案
        送进另一个问题——那会让模型拿着一个完全不相干的决定往下走，而界面一片安静。
        不传就不校验（一次只有一个待回答时，单靠"有没有挂着"已经够了）。
        """
        if not self.has_pending or self._pending is None:
            return False
        if question is not None and question.strip() != self._question:
            return False
        note = (text or "").strip()
        picked = (choice or "").strip()
        if note:
            self._pending.set_result(AskUserAnswer(choice=picked or None, text=note, kind="custom"))
        elif picked:
            self._pending.set_result(AskUserAnswer(choice=picked, text="", kind="choice"))
        else:
            return False
        return True

    def abandon(self, reason: str = "") -> bool:
        """没等来回答（窗口关闭 / 用户没理），按"未答"收口，绝不冒充用户选了什么。

        ⚠️ **当前 web 台尚未接线**：关掉浏览器走的是整轮取消（协程收到 CancelledError，
        主循环补一条"已被用户取消"的失败结果，与 Claude Code 一致），本方法在生产路径上
        还没有调用者。它留待"用户只是把卡片关掉、希望这一轮自己接着办完"那个场景
        ——那时应该在断开处理里调它，并且**不要**紧接着硬取消，否则
        ``status="no_answer"`` 那句人话根本来不及写进会话账本。
        在此之前它不是死代码：它是三档契约里"没等来回答"那一档的唯一出口，且有测试守着。
        """
        if not self.has_pending or self._pending is None:
            return False
        self._pending.set_result(AskUserAnswer(choice=None, text=reason, kind="abandoned"))
        return True


class AskUserTool(ReadOnlyTool):
    """向用户提一个问题。

    只读身份（R10）：不写任何用户数据，因此允许与同批其他只读工具并发——
    它挂起等待时不应拖住别的事。
    """

    name = "ask_user"
    # 需要主循环豁免工具超时：别的工具 2 分钟没干完就当失败，但"等用户"可能等很久。
    # 运行时据此在 _run_one 里不套 asyncio.wait_for 的 timeout（取消路径照旧收得住）。
    interactive: ClassVar[bool] = True

    description = (
        "当你卡在一个**确实该由用户拍板**的决定上、且这个决定没法从他的话里、从已有材料里"
        "或者从常理默认值推出来时，用这个工具问他一句。"
        "能合理猜的就猜着做：有常规默认值的、你自己能查明白的，直接选最显然的那个，在回答里"
        "提一句，然后继续——不要拿这种事打断用户。"
        "参数：question 是一句以问号结尾的话，把要他决定的事说清楚；options 给 2 到 4 个选项，"
        "每个选项 label 是 1 到 5 个词的短标签、description 是一句说明这么选会怎样。"
        "如果你偏爱某一项，把它放第一个，并在标签后加 (Recommended)。"
        "不要自己造“其他”或“自定义”选项——界面会自动给用户一个自己输入的口子。"
        "**不许用正文列选项**：要问就用这个工具弹卡片，把选项写在正文里等于让用户自己打一遍。\n"
        "一次只问一个问题；拿到答案后继续原来的任务，不要再问一遍。"
        "不要问他已经说过、或你能从他给的信息里推断出来的事——把刚说过的话再问一遍，"
        "比不问更糟。"
        "也不要拿它问你自己工作的确认（“这样行吗”“要不要继续”“我的计划可以吗”）："
        "该做就做，做完给他看结果。"

        "他要你**安排学习/做计划**时，先确认前提（什么时候考、每天能投入多久、先补哪块）——"
        "**只问他说过的之外还缺的那几项**，他前面说过的不许再问；"
        "**不许替他编数字**（“每天 30 分钟”“每天 20 个词”这类必须是他说的）；"
        "确实还缺、他又不肯说，就给两个具体方案让他挑。\n"
        "他答“还没定”“随便”“你看着办”**也算答过了**，不许换个说法再问一次；"
        "确实还缺关键信息时，给他两个具体做法让他挑，而不是替他选一个。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要用户决定的那件事，一句话，以问号结尾。",
            },
            "options": {
                "type": "array",
                "description": (
                    "2 到 4 个互斥选项，推荐项放第一个并加 (Recommended) 后缀。"
                    "数量上限由工具在运行时校验并回话纠正，不放进这里的硬约束。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "1 到 5 个词的短标签。"},
                        "description": {
                            "type": "string",
                            "description": "一句话说明这么选会怎样、有什么取舍。",
                        },
                    },
                    "required": ["label", "description"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["question", "options"],
        "additionalProperties": False,
    }

    def __init__(self, gateway: AskUserGateway) -> None:
        self._gateway = gateway

    @property
    def gateway(self) -> AskUserGateway:
        """装配层注入的回答通道（服务层据此确认自己拿的是同一个实例）。"""
        return self._gateway

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question", "")).strip()
        raw_options = args.get("options") or []

        if not question:
            return _steer(
                "问题不能是空的。请用一句话把要用户决定的事说清楚，以问号结尾，再调一次。"
            )

        options: list[dict[str, str]] = []
        for item in raw_options:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            if not label:
                return _steer("有个选项没写标签。每个选项都要有 label 和 description。")
            options.append(
                {"label": label, "description": str(item.get("description", "")).strip()}
            )

        if len(options) < MIN_OPTIONS:
            if len(options) == 1:
                return _steer(
                    "你这次只给了 1 个选项，所以没有发出去，用户没看到——"
                    "只有一个选项的问题里不存在“选择”。"
                    "不要编造凑数的第二个选项；如果你其实只有一条路，"
                    "直接说明你要这么做，然后继续原来的任务。"
                )
            return _steer(
                f"至少要给 {MIN_OPTIONS} 个选项才构成一次选择（你一个都没给）。"
                "把可能的做法列出来再问；如果只有一条路，直接说你要怎么做，别问。"
            )

        if len(options) > MAX_OPTIONS:
            return _steer(
                f"一次最多 {MAX_OPTIONS} 个选项（你给了 {len(options)} 个）。"
                "把相近的合并、或者只保留最值得权衡的几个，再问一次。"
            )

        seen: set[str] = set()
        for opt in options:
            if opt["label"] in seen:
                return _steer(
                    f"有两个选项的标签重复了（都叫「{opt['label']}」），用户会分不清。"
                    "请改成彼此不同的说法。"
                )
            seen.add(opt["label"])

        if self._gateway.has_pending:
            return _steer(
                "上一个问题还没等到用户的回答，一次只能问一个。"
                "等这次有了答案，再问下一个。"
            )

        answer = await self._gateway.ask(question=question, options=options)

        if answer.kind == "abandoned":
            why = f"（{answer.text}）" if answer.text else ""
            return {
                "status": "no_answer",
                "choice": None,
                "custom_text": "",
                "summary": (
                    f"没有等到用户的回答{why}。不要重复问同一个问题，"
                    "请根据已有的信息自己判断，继续把事情办完。"
                ),
            }

        if answer.kind == "custom":
            return {
                "status": "answered",
                "choice": answer.choice,
                "custom_text": answer.text,
                "summary": (
                    f"用户自己说明了：「{answer.text}」。仔细读——"
                    "他可能是在改需求、补充情况，或者不让你这么做，照他说的办。"
                ),
            }

        return {
            "status": "answered",
            "choice": answer.choice,
            "custom_text": "",
            "summary": f"用户选了「{answer.choice}」。带着这个答案继续，不要再问一遍。",
        }


def _steer(summary: str) -> dict[str, Any]:
    """参数不合规时的统一出口：不是失败，是一句教模型改的话。"""
    return {"status": "steer", "summary": summary}
