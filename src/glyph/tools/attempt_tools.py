"""学习尝试记录三件套：record_attempt / read_attempts / delete_attempt（T07）。

- record_attempt（**写入**）：一次短练习判出结果、**用户明确确认保存**后，记一条
  可核对的尝试。confirmation 必须是用户同意的原话——模型自我总结不能冒充确认；
- read_attempts（**只读**）：继续练习 / 安排学习时，按知识点 + 最近时间读有限几条，
  跨会话可见，用来出不重复、不泄露答案的新题；普通批改 / 查词 / 闲聊不读；
- delete_attempt（**写入**）：用户明确要求时软删一条（进回收站、可追溯）。
  不提供"原地改答案"：更正 = 用户确认删旧条 + 再确认记新条，绝不静默改写原始作答。

R10 身份：record / delete 继承 WriteTool（批次串行），read 继承 ReadOnlyTool（可并发）。
三工具共用装配根注入的同一个 AttemptStore。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..attempts import (
    DEFAULT_READ_LIMIT,
    DEFAULT_TOTAL_MAX_BYTES,
    AttemptError,
    AttemptNotFoundError,
    AttemptStore,
    build_attempt,
)
from ..runtime.errors import ToolError
from .base import ReadOnlyTool, WriteTool

__all__ = ["RecordAttemptTool", "ReadAttemptsTool", "DeleteAttemptTool"]

_OUTCOME_ZH = {
    "independent_correct": "独立做对",
    "correct_after_hint": "提示后做对",
    "needs_practice": "仍需练习",
}


class RecordAttemptTool(WriteTool):
    name = "record_attempt"
    description = (
        "当且仅当一次短练习（你出的小题 / 追问练习）已经判出结果、**且用户已明确同意"
        "把这次尝试保存下来**时才调用。confirmation 必须逐字填用户同意保存的原话"
        "（如“好的，记下来”“确认保存”），它是用户确认的唯一凭据。\n"
        "- skill：这次练的知识点（自由文本，如“一般过去时”“冠词 a/the”，不局限于六类硬错码）；\n"
        "- prompt：题面原样；answer：用户实际作答逐字，不要替他改正；\n"
        "- hint_used：这次有没有给过提示；\n"
        "- outcome 三选一：independent_correct（没要提示一次答对）/ "
        "correct_after_hint（给过提示后才答对）/ needs_practice（提示后仍错或还要练）；\n"
        "- evidence：一句话判定依据（正确答案 / 错在哪）。\n"
        "不要用于：用户还没确认保存、只是你自己总结完想顺手记录、普通批改 / 查词 / 闲聊——"
        "这些情况不要调用；你的自我总结不能代替用户确认，也不要凭单条结果声称用户已掌握。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "本次练习的知识点", "minLength": 1},
            "prompt": {"type": "string", "description": "题面原样", "minLength": 1},
            "answer": {
                "type": "string",
                "description": "用户实际作答，逐字，不要替他改正",
                "minLength": 1,
            },
            "hint_used": {
                "type": "boolean",
                "description": "这次是否给过提示",
                "default": False,
            },
            "outcome": {
                "type": "string",
                "description": "本次结果三档之一",
                "enum": [
                    "independent_correct",
                    "correct_after_hint",
                    "needs_practice",
                ],
            },
            "evidence": {
                "type": "string",
                "description": "一句话判定依据：正确答案 / 错在哪",
                "minLength": 1,
            },
            "confirmation": {
                "type": "string",
                "description": "用户明确同意保存这次尝试的原话；没有用户确认就不要调用本工具",
                "minLength": 1,
            },
        },
        "required": ["skill", "prompt", "answer", "outcome", "evidence", "confirmation"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        store: AttemptStore,
        *,
        session_id_provider: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._session_id_provider = session_id_provider

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        sid = self._session_id_provider()
        if not sid or not str(sid).strip():
            raise ToolError(
                "当前没有活动会话，无法把这次练习记到具体会话；请在一个进行中的会话里"
                "继续练习、拿到用户确认后再记录。"
            )

        try:
            record = build_attempt(
                session_id=sid,
                skill=args.get("skill"),
                prompt=args.get("prompt"),
                answer=args.get("answer"),
                hint_used=args.get("hint_used", False),
                outcome=args.get("outcome"),
                evidence=args.get("evidence"),
                confirmation=args.get("confirmation"),
            )
            action, saved = self._store.save(record)
        except AttemptError as exc:
            raise ToolError(f"保存练习尝试失败：{exc}") from exc

        if action == "exists":
            note = "这次练习尝试之前已经保存过（同一题、同一份作答），没有重复记录。"
        else:
            note = (
                f"已保存这次练习尝试（结果：{_OUTCOME_ZH[saved.outcome.value]}）。"
                "它只在用户下次继续练习 / 安排复习时被读取，普通批改、查词、闲聊不会翻它；"
                "不要仅凭这一条就说用户已经掌握。"
            )
        return {
            "action": action,
            "attempt_id": saved.attempt_id,
            "skill": saved.skill,
            "outcome": saved.outcome.value,
            "note": note,
        }


class ReadAttemptsTool(ReadOnlyTool):
    name = "read_attempts"
    description = (
        "当且仅当用户要继续练习、复习，或让你安排学习内容时才调用，读取他在某知识点上"
        "最近几次的练习尝试，据此出**不重复、不直接泄露旧答案**的新题。skill 可填知识点"
        "做相关性过滤（泛主题能命中带修饰的具体记录），不确定就留空先看最近的；limit 是你"
        "想看的条数（系统会自动夹到上限）。"
        "不要用于普通批改、查词、翻译或闲聊，那些场景不要调用、也不要翻旧练习。"
        "返回 count 为 0 时按 note 如实说没有历史，**不要编造“上次练过什么”**。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "按知识点相关性过滤（泛主题可命中带修饰的具体记录）",
                "minLength": 1,
            },
            "limit": {
                "type": "integer",
                "description": "想读取的最近条数（会被系统夹到安全上限）",
                "minimum": 1,
            },
        },
        "additionalProperties": False,
    }

    def __init__(
        self,
        store: AttemptStore,
        *,
        read_limit: int = DEFAULT_READ_LIMIT,
        total_max_bytes: int = DEFAULT_TOTAL_MAX_BYTES,
    ) -> None:
        self._store = store
        self._read_limit = read_limit
        self._total_max_bytes = total_max_bytes

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_skill = args.get("skill")
        skill = str(raw_skill).strip() if isinstance(raw_skill, str) and raw_skill.strip() else None

        raw_limit = args.get("limit")
        if isinstance(raw_limit, int) and raw_limit >= 1:
            limit = min(raw_limit, self._read_limit)
        else:
            limit = self._read_limit

        page = self._store.list(skill, limit=limit, total_max_bytes=self._total_max_bytes)

        attempts = [
            {
                "attempt_id": rec.attempt_id,
                "skill": rec.skill,
                "outcome": rec.outcome.value,
                "hint_used": rec.hint_used,
                "confirmed_at": (rec.confirmed_at or rec.created_at).isoformat(),
                "prompt": rec.prompt,
                "answer": rec.answer,
            }
            for rec in page.records
        ]

        if not attempts:
            scope = f"「{skill}」" if skill else "任何知识点"
            note = (
                f"没有找到{scope}的练习尝试记录。不要假设用户以前练过、也不要复述并不存在的"
                "“上次练习”；就当第一次，直接出一道新题。"
            )
        else:
            note = (
                f"已给出最近 {len(attempts)} 次练习尝试，供你出题时避开重复。"
                "回灌里**没有正确答案**（只有题面和他当时的原错答），所以既不要照念、"
                "也不要自己推断后把旧题的正确形式说出来——承接时只提知识点主题"
                "（如“上次练了一般过去时”），新题的答案让他自己想。"
            )
            if page.truncated:
                note += " 还有更早的记录没展示（受条数 / 上下文预算限制），需要可缩小知识点再读。"
            if page.skipped_files:
                note += f" 另有 {page.skipped_files} 条记录无法读取已跳过。"

        return {
            "skill": skill,
            "count": len(attempts),
            "truncated": page.truncated,
            "skipped_files": page.skipped_files,
            "attempts": attempts,
            "note": note,
        }


class DeleteAttemptTool(WriteTool):
    name = "delete_attempt"
    description = (
        "当且仅当用户明确要求删掉某一条练习尝试记录时才调用，confirmation 逐字填他要求"
        "删除的原话。删除是软删（移入回收站、可追溯），不会改动其它记录。"
        "不要用于你自己觉得某条记错了就偷偷删；本工具**不提供修改答案**——若确需更正，"
        "先经用户确认删掉旧条，再让用户确认重新记录一条新的。用户没明确要求删除时不要调用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "attempt_id": {
                "type": "string",
                "description": (
                    "要删除的尝试记录编号（record_attempt / read_attempts 返回的 12 位 id）"
                ),
                "minLength": 1,
            },
            "confirmation": {
                "type": "string",
                "description": "用户明确要求删除这条记录的原话",
                "minLength": 1,
            },
        },
        "required": ["attempt_id", "confirmation"],
        "additionalProperties": False,
    }

    def __init__(self, store: AttemptStore) -> None:
        self._store = store

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        attempt_id = str(args.get("attempt_id", "")).strip()
        confirmation = str(args.get("confirmation", "")).strip()
        if not confirmation:
            raise ToolError("删除练习记录必须有用户明确要求删除的原话（confirmation）。")

        try:
            self._store.soft_delete(attempt_id)
        except AttemptNotFoundError as exc:
            raise ToolError(f"没有这条练习尝试记录，无法删除：{attempt_id}") from exc
        except AttemptError as exc:
            raise ToolError(f"删除练习尝试失败：{exc}") from exc

        return {
            "attempt_id": attempt_id,
            "deleted": True,
            "note": "已把这条练习尝试移入回收站（可追溯），其它记录没有改动。",
        }
