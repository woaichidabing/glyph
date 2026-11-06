"""记忆三件套：remember / forget / read_memory。

- remember（写入）：内容照用户原话存，模型负责分类（kind）与一句话钩子（title）；
  key 内容寻址，同内容重复提只累加次数；
- forget（写入）：按 key 软删（进 .trash，可找回）；
- read_memory（只读）：按关键词在 title / 原话等字段里做归一化子串匹配，
  单条正文按字节预算截断。

三件套与目录注入器必须共用**同一个 MemoryStore 实例**（装配根负责）。
"""

from __future__ import annotations

import re
from typing import Any

from ..memory.body import render_body
from ..memory.errors import MemoryEntryNotFoundError, MemoryError
from ..memory.mastery import (
    CODE_LABELS_ZH,
    MasteryPoint,
    MasteryStage,
    ReviewVerdict,
    apply_point_to_entry,
    enroll,
    normalize_error_code,
    point_from_entry,
    record_user_claim,
    record_verdict,
    reopen,
)
from ..memory.sanitize import clip_bytes, sanitize_text
from ..memory.slug import compute_key, normalize_fragment
from ..memory.store import MemoryStore
from ..memory.types import MemoryEntry, MemoryType
from ..runtime.errors import ToolError
from .base import ReadOnlyTool, WriteTool

__all__ = [
    "ConfirmMasteryTool",
    "ForgetTool",
    "ReadMemoryTool",
    "RecordReviewResultTool",
    "RememberTool",
]

_KEY12 = re.compile(r"^[0-9a-f]{12}$")
_MAX_MATCHES = 10


class RememberTool(WriteTool):
    name = "remember"
    description = (
        "当且仅当用户明确要求你记住一句话（用户说“记住：……”）时才调用，"
        "把内容逐字存进长期记忆、不要改写成你的总结；同时给出 kind 分类和一句 ≤40 字的 title 钩子。"
        "记错题（kind=mistake）时顺手把 original（错的原句）和 correction（改法）也传进来——"
        "它们是这条错题的身份，同一个错换讲解不会被记成两条。"
        "不要用于：用户只是随口一说、没有要你记，也不要把同一句话反复存（重复内容会自动累加次数）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要记住的原话，逐字，不要翻译或总结",
                "minLength": 1,
            },
            "kind": {
                "type": "string",
                "description": "记忆类型；拿不准时用 preference",
                "enum": ["mistake", "preference", "feedback", "fact"],
            },
            "title": {
                "type": "string",
                "description": "目录页用的一句话钩子，≤40 字",
            },
            "original": {
                "type": "string",
                "description": (
                    "仅 mistake：错的原文片段（如 I go to school yesterday）。"
                    "给了它，同一个错换个讲解仍算同一条"
                ),
            },
            "correction": {
                "type": "string",
                "description": (
                    "仅 mistake：建议改法（如 I went to school yesterday）。与 original 成对给"
                ),
            },
            "error_code": {
                "type": "string",
                "description": (
                    "仅 kind=mistake、且这是用户已采纳的客观语法硬错时才给，取 6 类标准码之一："
                    "VERB:TENSE / MORPH / NOUN:NUM / NOUN:INFL / DET / SPELL。"
                    "中式英语、啰嗦、建议类，以及介词、标点、句子结构、语序等不在 6 类内的，"
                    "或你拿不准的，一律省略这个参数（不要硬凑）。给了合法码会为该知识点建立 / "
                    "更新复习账本，之后自动安排针对性复测。"
                ),
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    }

    def __init__(self, store: MemoryStore, *, title_max_chars: int = 40) -> None:
        self._store = store
        self._title_max_chars = title_max_chars

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        content = sanitize_text(str(args.get("content", ""))).strip()
        if not content:
            raise ToolError("缺少要记住的内容（content），或内容全是空白 / 控制符")

        kind_raw = str(args.get("kind") or "preference")
        try:
            kind = MemoryType(kind_raw)
        except ValueError as exc:
            raise ToolError(
                f"kind 必须是 mistake / preference / feedback / fact 之一：{kind_raw!r}"
            ) from exc

        title_raw = str(args.get("title") or "").strip()
        title = title_raw or content.replace("\n", " ")
        if len(title) > self._title_max_chars:
            title = title[: self._title_max_chars - 1] + "…"

        # mistake 的身份字段：给了原文 / 改法就用它们（换讲解不算新错），
        # 没给就退化用整条正文兜底——否则所有错题会算成同一个 key 互相覆盖
        original = sanitize_text(str(args.get("original") or "")).strip() or None
        correction = sanitize_text(str(args.get("correction") or "")).strip() or None
        # 知识点代码：只在 mistake 且命中 6 类白名单时生效；脏码 / 非错题一律 None（D17）
        normalized_code = normalize_error_code(args.get("error_code"))
        code = normalized_code if kind is MemoryType.MISTAKE else None

        key = compute_key(
            kind, title=title, original=original, correction=correction, content=content
        )
        entry = MemoryEntry(
            key=key,
            type=kind,
            title=title,
            quote=content,
            count=1,  # 占位；store.upsert 按是否命中决定真实次数
            first_seen="",
            last_seen="",
            last_mentioned_at="",
        )
        if kind is MemoryType.MISTAKE:
            entry.original = original
            entry.correction = correction
            if code:
                entry.error_code = code  # 同码证据句也带身份，删代表时靠它过户（D15/D20）

        # 建账 / 复活判断必须在写入本句之前：此刻查到的是"此前"的账本代表
        existing_point = self._store.find_mastery_point_by_code(code) if code else None
        try:
            action = self._store.upsert(entry)
            mastery = self._apply_mastery(code, key, existing_point)
        except MemoryError as exc:
            raise ToolError(f"记忆写入失败：{exc}") from exc

        result = {
            "action": action,
            "key": key,
            "kind": kind.value,
            "title": title,
            "total_entries": len(self._store.list_entries()),
        }
        if mastery is not None:
            result["mastery"] = mastery
        return result

    def _apply_mastery(
        self,
        code: str | None,
        key: str,
        existing_point: MasteryPoint | None,
    ) -> dict[str, Any] | None:
        """定稿采纳硬错后的账本三分支（D10 / D15 / D18）；无合法 code 返回 None。

        - 无代表（enrolled）：本句成为代表，建档 new、次日首考；
        - 代表已掌握（reopened）：复活原代表回 learning、连对清零、次日重走；
        - 代表学习中（tracking）：账本进度一律不动，本句已作为带码证据落盘。
        """
        if not code:
            return None
        today = self._store.clock().date()
        if existing_point is None:
            point = enroll(key, code, today)
            ledger = apply_point_to_entry(point, self._store.get(key))
            self._store.save_mastery(ledger)
            ledger_action, stage, due = "enrolled", point.stage, point.due_date
        elif existing_point.stage is MasteryStage.MASTERED:
            point = reopen(existing_point, today)
            representative = self._store.get(existing_point.key)
            self._store.save_mastery(apply_point_to_entry(point, representative))
            ledger_action, stage, due = "reopened", point.stage, point.due_date
        else:
            # 学习中又犯同一知识点：不清零、不 +wrong、不动到期，只加证据（D18）
            ledger_action, stage, due = (
                "tracking",
                existing_point.stage,
                existing_point.due_date,
            )
        return {
            "action": ledger_action,
            "error_code": code,
            "error_code_zh": CODE_LABELS_ZH[code],
            "stage": stage.value,
            "due_date": due.isoformat() if due is not None else None,
        }


class RecordReviewResultTool(WriteTool):
    """复测记账（硬动作）：判完档**必须**调它，档位 / 到期 / 连对由状态机纯函数算。

    模型只能给"哪条（key）、判定档、题干、用户答案、讲解"，碰不到 mastery_stage /
    due_date / independent_streak 这些字段——工具内部读出已建档错题、跑状态机、走唯一
    掌握度写入口落盘，并在 sidecar 留一条复测流水。
    """

    name = "record_review_result"
    description = (
        "用户做完一道针对到期知识点的复习题、你判定档位后，**必须立刻调用**一次，"
        "把这次复测记进掌握度账本。\n"
        "- key：到期复习行里 `key=` 后面那 12 位编号；\n"
        "- verdict：independent=用户独立做对，hinted=给过提示后才对，wrong=没做对；\n"
        "- question：这次出的题干原样；answer：用户这次实际给出的答案逐字；\n"
        "- explanation：讲解 / 标准答案，判 wrong 时务必给正确答案与原因。\n"
        "下次到期日、连对次数、是否已掌握由系统按复习规则计算，你**不能也不要**自己传这些。"
        "已掌握的点再记会报错，属正常（掌握之后只有又在作文里犯同一错才会自动复活）。"
        "不要用于：还没出题、还没拿到用户答案，或这不是针对到期知识点的复测。"
        "判档流程（前两步不许落盘）：\n"
        "① 第一次作答就答对（没给过提示）→ verdict=independent；\n"
        "② 第一次答错、或他说“不会”→ **这一轮先不调用本工具**：只给一个指向性提示"
        "（点出线索、但不给正确词形），让他再试一次；\n"
        "③ 给过提示后第二次答对 → verdict=hinted；仍错、放弃、或要求直接看答案 → "
        "verdict=wrong（这时才讲透并给标准答案）。\n"
        "出题要求（题目质量就靠这几条）：\n"
        "· 以到期行里的历史错句为种子，换主语 / 换动词 / 换语境，只出 1 道；\n"
        "· 填空型：用 ___ 挖空，括号里只给需要变形的原形，正确答案必须与括号词形不同；\n"
        "· 词形不变的点（不可数 information / advice / progress）和拼写，必须用改错型"
        "（给 informations、recieve 这类错词让改）；\n"
        "· 题面任何位置都不能出现正确答案；改错句必须真的含一个该考点的错，"
        "改完的整句必须完全正确、不能引入新错（many advices → much advice）；\n"
        "· a/an 看后面词的发音（an honest / an hour，a university / a UFO）。\n"
        "④ 他当场反驳判定时，据题独立复核一次；确属你判错就改判并说明，"
        "有争议的答案绝不能落成 wrong。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "到期复习行里 key= 后的 12 位十六进制记忆编号",
                "minLength": 12,
                "maxLength": 12,
            },
            "verdict": {
                "type": "string",
                "description": "本次复测判定档",
                "enum": ["independent", "hinted", "wrong"],
            },
            "question": {"type": "string", "description": "这次出的题（题干原样）", "minLength": 1},
            "answer": {
                "type": "string",
                "description": "用户这次实际给出的答案（逐字，不要替他改正）",
                "minLength": 1,
            },
            "explanation": {
                "type": "string",
                "description": "讲解 / 标准答案；判 wrong 时必填，写清正确答案与原因",
            },
        },
        "required": ["key", "verdict", "question", "answer"],
        "additionalProperties": False,
    }

    _VERDICT_ZH = {"independent": "独立做对", "hinted": "提示后做对", "wrong": "没做对"}

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key", "")).strip()
        if not _KEY12.match(key):
            raise ToolError(f"key 必须是到期复习行里 key= 后的 12 位十六进制编号：{key!r}")

        verdict_raw = str(args.get("verdict", "")).strip()
        try:
            verdict = ReviewVerdict(verdict_raw)
        except ValueError as exc:
            raise ToolError(
                f"verdict 必须是 independent / hinted / wrong 之一：{verdict_raw!r}"
            ) from exc

        question = sanitize_text(str(args.get("question", ""))).strip()
        answer = sanitize_text(str(args.get("answer", ""))).strip()
        if not question or not answer:
            raise ToolError("记账需要非空的 question（题干）与 answer（用户答案）")
        explanation = sanitize_text(str(args.get("explanation", ""))).strip()

        try:
            existing = self._store.get(key)
        except MemoryEntryNotFoundError as exc:
            raise ToolError(
                f"没有 key 为 {key} 的错题记忆，无法记账；请确认 key 取自到期复习行"
            ) from exc

        point = point_from_entry(existing)
        if point is None:
            raise ToolError(
                "这条错题还没进入复习账本（没有掌握度状态），不能记复测结果；"
                "它得先在批改定稿时被采纳、建档后才会有复测"
            )

        today = self._store.clock().date()
        try:
            new_point = record_verdict(point, verdict, today)
        except ValueError as exc:
            # 已掌握点再记会被状态机拒绝：须走"又犯同一错→复活"，不能在这儿偷偷推进
            raise ToolError(f"这个知识点已经掌握了，不能直接记复测：{exc}") from exc

        updated = apply_point_to_entry(new_point, existing)
        record = {
            "date": today.isoformat(),
            "verdict": verdict.value,
            "question": question,
            "answer": answer,
            "explanation": explanation,
        }
        try:
            self._store.save_mastery(updated)
            self._store.append_review(key, record)
        except MemoryError as exc:
            raise ToolError(f"复测结果落盘失败：{exc}") from exc

        mastered = new_point.stage is MasteryStage.MASTERED
        due = new_point.due_date.isoformat() if new_point.due_date is not None else None
        if mastered:
            note = (
                f"已连续独立做对 {new_point.streak} 次，「{new_point.error_code}」已掌握，"
                "以后不再主动安排复测（若以后作文里又犯同一错会自动复活）。"
            )
        else:
            note = f"已记录本次复测（{self._VERDICT_ZH[verdict.value]}），下次复测安排在 {due}。"
        return {
            "key": key,
            "error_code": new_point.error_code,
            "verdict": verdict.value,
            "stage": new_point.stage.value,
            "independent_streak": new_point.streak,
            "due_date": due,
            "mastered": mastered,
            "good_count": new_point.good_count,
            "hard_count": new_point.hard_count,
            "wrong_count": new_point.wrong_count,
            "note": note,
        }


class ConfirmMasteryTool(WriteTool):
    """用户自述「我会了」→ 出题验证 → 已掌握（D31）。

    与 record_review_result 的区别（两条路都通，但依据不同）：
    - record_review_result：到期复测的常规路径，判档走 record_verdict，掌握记 streak；
    - confirm_mastery：用户自己说会了，模型先 read_memory 取出那条的历史错句，
      换语境出**一道**题，答对才算掌握，记 user_claim。

    为什么出一道题而不是直接信：完全不信用户会烦（"我都说了我会了"）；
    完全信容易被高估、而且他以后再犯时系统认不出是复发。一道题是折中。

    答错**不惩罚**：保持原状态，复测阶梯继续，只是这次不算掌握。
    """

    name = "confirm_mastery"
    description = (
        "用户**自己说**某个知识点已经会了（「我早会了」「别再考我这个了」「这个我懂」）时用。"
        "流程：先从记忆里读出那条错题的**历史错句**（用 read_memory 按关键词读），"
        "然后以它为种子**换主语 / 换动词 / 换语境出 1 道同考点新题**，"
        "让用户做；他答完你判对错，再调本工具。"
        "\n- key：那条错题记忆的 12 位编号；"
        "\n- question：这次出的题（题干原样）；answer：用户实际答案（逐字，不要替他改正）；"
        "\n- correct：他答对了没有（true/false）。这是他自述掌握的**唯一凭据**，"
        "答错就传 false——**不许**因为他嘴上说会了就传 true；"
        "\n- explanation：讲解 / 正确答案，判 false 时务必给。"
        "答对 → 标为已掌握，以后不再安排复测（若以后作文里又犯同一错会自动复活）；"
        "答错 → 保持原状态，复测阶梯继续，本次不算数。"
        "不要用于：到期复测判档（那走 record_review_result）；或用户还没做题你就替他判定。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "那条错题记忆的 12 位十六进制编号",
                "minLength": 12,
                "maxLength": 12,
            },
            "question": {
                "type": "string",
                "description": "这次出的验证题（题干原样）",
                "minLength": 1,
            },
            "answer": {
                "type": "string",
                "description": "用户这次实际给出的答案（逐字，不要替他改正）",
                "minLength": 1,
            },
            "correct": {
                "type": "boolean",
                "description": "他答对了没有；答错必须传 false",
            },
            "explanation": {
                "type": "string",
                "description": "讲解 / 正确答案；答错时必填",
            },
        },
        "required": ["key", "question", "answer", "correct"],
        "additionalProperties": False,
    }

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key", "")).strip()
        if not _KEY12.match(key):
            raise ToolError(f"key 必须是那条错题记忆的 12 位十六进制编号：{key!r}")

        question = sanitize_text(str(args.get("question", ""))).strip()
        answer = sanitize_text(str(args.get("answer", ""))).strip()
        if not question or not answer:
            raise ToolError("确认掌握需要非空的 question（题干）与 answer（用户答案）")

        correct = bool(args.get("correct"))
        explanation = sanitize_text(str(args.get("explanation", ""))).strip()

        try:
            existing = self._store.get(key)
        except MemoryEntryNotFoundError as exc:
            raise ToolError(f"没有 key 为 {key} 的错题记忆，无法确认掌握") from exc

        point = point_from_entry(existing)
        if point is None:
            raise ToolError(
                "这条错题还没进入复习账本（没有掌握度状态）；"
                "它得先在批改定稿时被采纳、建档后才有掌握度可谈"
            )

        today = self._store.clock().date()

        # 已经掌握：如实说明，不重复记账、也不报错（用户可能只是又说了一遍）
        if point.stage is MasteryStage.MASTERED:
            return {
                "key": key,
                "error_code": point.error_code,
                "mastered": True,
                "mastered_by": existing.mastered_by or "streak",
                "due_date": None,
                "note": "这个知识点已经掌握了，无需再确认——以后不再主动安排复测。",
            }

        new_point = record_user_claim(point, today, verified=correct)
        updated = apply_point_to_entry(new_point, existing)

        record = {
            "date": today.isoformat(),
            "verdict": "user_claim",
            "question": question,
            "answer": answer,
            "explanation": explanation,
            "correct": correct,
        }
        # 答错也留一条流水：以后回看能知道"他什么时候说过会了、当时验出来不会"
        try:
            if correct:
                self._store.save_mastery(updated)
            self._store.append_review(key, record)
        except MemoryError as exc:
            raise ToolError(f"确认掌握落盘失败：{exc}") from exc

        if correct:
            note = (
                "这道题答对了，已标为已掌握（依据：用户自述 + 验证通过），"
                "以后不再主动安排复测（若以后作文里又犯同一错会自动复活）。"
            )
        else:
            # 答错不惩罚：状态机没被推进，复测日程照旧（due_date 由返回值如实带出）
            note = "这道题没答对，所以先不算掌握——保持原状态，复测阶梯继续。"
        return {
            "key": key,
            "error_code": new_point.error_code,
            "mastered": new_point.stage is MasteryStage.MASTERED,
            "mastered_by": new_point.mastered_by,
            "due_date": new_point.due_date.isoformat() if new_point.due_date else None,
            "note": note,
        }


class ForgetTool(WriteTool):
    name = "forget"
    description = (
        "按 key 把一条长期记忆忘掉（软删进回收站、可找回）。"
        "当且仅当用户明确说“忘掉 / 别再记……”某条记忆时调用；"
        "不知道 key 时先用 read_memory 找到它。不要用于：用户没点名要删，"
        "或只是想让你这轮别提它。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "要忘掉的记忆编号（12 位十六进制）",
                "minLength": 12,
                "maxLength": 12,
            }
        },
        "required": ["key"],
        "additionalProperties": False,
    }

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key", "")).strip()
        if not _KEY12.match(key):
            raise ToolError(f"key 必须是 12 位十六进制编号：{key!r}")
        try:
            self._store.soft_delete(key)
        except MemoryEntryNotFoundError as exc:
            raise ToolError(f"没有这条记忆，没法忘掉：{key}") from exc
        except MemoryError as exc:
            raise ToolError(f"记忆删除失败：{exc}") from exc
        return {"key": key, "forgotten": True}


class ReadMemoryTool(ReadOnlyTool):
    name = "read_memory"
    description = (
        "读取长期记忆里某条的具体内容。两种给法，二选一：\n"
        "1. 给 `key` —— 目录里已经指到某一条时（目录每行形如“[类型] key 标题”），"
        "直接用 key 一次取到正文，**不要**再用关键词反复试探；\n"
        "2. 给 `query` —— 只知道大概内容、不知道 key 时，用关键词在标题 / 原话 / "
        "原文片段 / 改法里匹配。\n"
        "当且仅当你要用到某条记忆的细节、或用户问“你都记了我什么”时才调用。"
        "不要用于：用户没提到记忆、你自己这轮也不需要；更不要凭目录页一行字猜细节——"
        "先读再说。找不到就如实说没有，不要编。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "记忆条目的 key（目录里 “[类型] key 标题” 中间那串）。"
                    "给了 key 就按它精确读取，不用猜关键词"
                ),
                "minLength": 1,
            },
            "query": {
                "type": "string",
                "description": "查找关键词（在标题 / 原话 / 原文片段 / 改法里匹配）",
                "minLength": 1,
            },
        },
        # 两个都可选、但至少要给一个：既保留旧的关键词用法，又支持明确定位（T04）
        "anyOf": [{"required": ["key"]}, {"required": ["query"]}],
        "additionalProperties": False,
    }

    def __init__(self, store: MemoryStore, *, body_max_bytes: int = 4096) -> None:
        self._store = store
        self._body_max_bytes = body_max_bytes

    def _pack(self, entry: MemoryEntry) -> dict[str, Any]:
        """把一条记忆渲染成返回项，正文按预算截断并标记（两种读法共用一份）。"""
        body = render_body(entry)
        clipped, truncated = clip_bytes(body, self._body_max_bytes)
        if truncated:
            # 提示语也占预算：重新截一次、给它留出位置，保证总字节不超
            note = "\n…（正文超出预算，已截断）"
            reserved = self._body_max_bytes - len(note.encode("utf-8"))
            clipped, _ = clip_bytes(body, max(0, reserved))
            clipped += note
        return {
            "key": entry.key,
            "kind": entry.type.value,
            "title": entry.title,
            "content": clipped,
            "truncated": truncated,
        }

    def _read_by_key(self, key: str) -> dict[str, Any]:
        """按 key 精确读取一条。找不到要说出来，不能静默当成功（T04）。"""
        try:
            entry = self._store.get(key)
        except MemoryEntryNotFoundError:
            return {
                "key": key,
                "count": 0,
                "matches": [],
                "note": (
                    f"没有 key 为 {key} 的记忆（可能从未记过、或已被删除）。"
                    "不要凭空补内容；需要的话可以向用户确认。"
                ),
            }
        return {"key": key, "count": 1, "matches": [self._pack(entry)]}

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        key = sanitize_text(str(args.get("key", ""))).strip()
        query = sanitize_text(str(args.get("query", ""))).strip()

        # 明确定位优先：目录已经指到某一条时，按 key 一次取到，不靠关键词试探（T04）
        if key:
            return self._read_by_key(key)
        if not query:
            raise ToolError("缺少定位条件：给 key（目录里那条）或 query（关键词）")

        needle = normalize_fragment(query)

        matches = []
        for entry in sorted(
            self._store.list_entries(),
            key=lambda e: (e.last_mentioned_at, e.count),
            reverse=True,
        ):
            haystack_parts = [entry.title, entry.quote]
            if entry.original:
                haystack_parts.append(entry.original)
            if entry.correction:
                haystack_parts.append(entry.correction)
            haystack = normalize_fragment("\n".join(haystack_parts))
            if needle in haystack:
                matches.append(self._pack(entry))
            if len(matches) >= _MAX_MATCHES:
                break

        return {"query": query, "count": len(matches), "matches": matches}
