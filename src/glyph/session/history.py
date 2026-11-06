"""会话短期记忆的唯一对外接口。

「一次连续对话 = 一个 SessionHistory」：它保管内存态的那份流水，负责落盘，
也负责把流水算成「这一轮该发给模型的消息」。主循环只用它的方法，不见任何文件操作。

两个入口：

- ``new_session()`` —— 开一个新会话（前端想显示空白页时）；
- ``open_session()`` —— 按 id 回到上次那一页（前端想显示上次那页时）。

``paths`` / ``records`` / ``store`` 都是本对象的内部实现，外界一概不认识。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONTEXT_DEDUPE, context_window_cap, load_settings
from ..context import ContextFragment, render_context_message
from ..types import (
    AssistantMessage,
    FinishReason,
    Message,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    Usage,
    UserMessage,
)
from .compaction import (
    ContextStats,
    composition_tokens,
    decide_compaction,
    dedupe_safely,
    estimate_tokens_from_bytes,
    measure,
)
from .compaction_l3 import (
    CompactionPlan,
    CompactionReport,
    EmergencyView,
    material_budget_bytes,
    render_emergency_resume,
    render_resume_user,
    select_material,
    select_tail,
    split_turns,
    tail_budget_bytes,
)
from .errors import SessionNotFoundError
from .packing import fold_artifact_reads, pack_artifacts, render_picklist, scan_grade_tasks
from .paths import generate_session_id, local_now, session_path
from .records import (
    APP_VERSION,
    SCHEMA_VERSION,
    AssistantMessagePayload,
    CancelPayload,
    CompactBoundaryPayload,
    FinalizePayload,
    ReasoningPayload,
    Record,
    RecordPayload,
    RecordType,
    SessionMeta,
    SummaryPayload,
    ToolCallPayload,
    ToolResultPayload,
    TurnEndPayload,
    UserMessagePayload,
    build_record,
    system_prompt_fingerprint,
)
from .replay import ReplayResult
from .store import SessionStore

_LOGGER = logging.getLogger(__name__)

# §10 / §14 应急失败时给用户的人话出路（合成话术，不含任何学习者 / 模型原话）。
_EMERGENCY_SINGLE_USER_TOO_LONG = (
    "这篇内容太长，一次处理不完。建议：① 把原文存成文件后用文件上传（长文本走文件通道、"
    "不占对话上下文）；② 分成上下两篇发给我。"
)
_EMERGENCY_OPEN_NEW_SESSION = (
    "这段对话太长了，我已经没法在不丢失内容的前提下继续。请新开一个会话再发；你之前的"
    "批改成品与定稿都完整保存在本会话记录里，不会丢，需要时可以随时回来调阅。"
)


class SyntheticKind(StrEnum):
    """``messages()`` 凭空补出来的那几类消息的**身份**。

    共同点只有一条：**它们只活在 ``messages()`` 的返回值里** —— 不写进流水、不出现在
    回放里、也不是任何真实发生过的对话内容。存在的理由各不相同（见行内注释），
    但性质一致：**模型需要看见它，而它从未发生过**。

    之所以单独立一个枚举、而不是随手写字面量，是为了让这个集合**可枚举**：新增一类时
    登记到这里，``tests/session/test_history.py`` 里「合成文案一律不落盘」那条测试
    就自动把它管住，不用记得回头补用例。

    同一件事在别处也有对应：Claude Code 叫 ``isMeta``（给模型看、不给用户看），
    Codex 则用 ``estimate_response_item_model_visible_bytes`` 度量它。两家都做了这个
    区分，只是没给它一个名字。
    """

    # tool_call 没等到结果时兜底闭合 —— 悬空会让千问直接报 400
    MISSING_TOOL_RESULT = "missing_tool_result"
    # 工具既没 output 也没 error，总得给模型一句话
    VOID_TOOL_RESULT = "void_tool_result"
    # D18：取消之后标一句「这里断过」，避免上下文里冒出连续两条 user
    INTERRUPTED_REPLY = "interrupted_reply"


SYNTHETIC_TEXTS: dict[SyntheticKind, str] = {
    SyntheticKind.MISSING_TOOL_RESULT: "无结果：会话中断或被取消",
    SyntheticKind.VOID_TOOL_RESULT: "工具没有返回内容",
    SyntheticKind.INTERRUPTED_REPLY: "（回复被中断，未说完）",
}
"""每种合成身份对应的固定文案（键值必须与 ``SyntheticKind`` 一一对应）。

**这里的文案一律不含任何用户或模型的原话** —— 是硬要求，不是排版偏好：一旦复述了
半截原文，批改场景下就等于把用户原文里的字原样送回模型，有人写一句「忽略前面的指令」
就能借这条通道完成提示注入。D18 的完整取舍与两个被否决的做法见 ``REFACTOR-DECISIONS.md``。
"""


def _tool_result_content(payload: ToolResultPayload) -> str:
    """工具结果的消息体：成功给 JSON 字符串，失败给人话原因。"""
    if payload.output is not None:
        return json.dumps(payload.output, ensure_ascii=False)
    return payload.error or SYNTHETIC_TEXTS[SyntheticKind.VOID_TOOL_RESULT]


class SessionHistory:
    """一次连续对话的全部短期记忆：事实流水的唯一所有者。

    主循环只「用」它：要发什么问 ``messages()``，发生了什么调 ``record_*()``。
    落盘、重建、将来的压缩切片都在本对象内部完成，调用方无需知道。

    请不要直接构造：用 ``new_session()`` / ``open_session()`` 两个工厂，它们负责把
    会话文件的「出生」和「续命」这两件事做对。
    """

    def __init__(
        self,
        *,
        store: SessionStore,
        meta: SessionMeta,
        system_prompt: str,
        records: list[Record] | None = None,
        system_prompt_mismatch: bool = False,
        skipped_lines: int = 0,
        clock: Callable[[], datetime] = local_now,
        context: Sequence[ContextFragment] = (),
        context_dedupe: bool = DEFAULT_CONTEXT_DEDUPE,
    ) -> None:
        self._store = store
        self._meta = meta
        self._system_prompt = system_prompt
        self._records: list[Record] = list(records) if records is not None else []
        self._system_prompt_mismatch = system_prompt_mismatch
        self._skipped_lines = skipped_lines
        self._clock = clock
        # 压缩 L1（去重）开关：默认从配置单点取，装配根可用环境变量关掉。
        self._context_dedupe = context_dedupe
        # 压缩 L2（成品装箱）渲染参数：构造时从配置单点快照一次（方案 §8，v2.1 #63）。
        # 只依赖产品档位（30 万 / 100 万），不 import llm（B2：session 不反向依赖模型层）。
        packing_settings = load_settings()
        self._auto_compact = packing_settings.auto_compact
        self._anchor_detail_recent = packing_settings.anchor_detail_recent
        self._task_idle_turns = packing_settings.task_idle_turns
        self._fold_keep_recent = packing_settings.fold_keep_recent
        self._artifact_result_keep_recent = packing_settings.artifact_result_keep_recent
        # 提货单字节预算 = W(token) × 4% 再按字节 / token 折算成 UTF-8 字节（A4）。
        window_tokens = context_window_cap(packing_settings.context_mode)
        self._l2_budget_bytes = max(
            1,
            int(
                window_tokens
                * packing_settings.anchor_budget_ratio
                * packing_settings.bytes_per_token
            ),
        )
        # C5 切档"解压"判定所需快照：有效窗口 W、L2 线比例、字节→token 系数与安全余量。
        # 渲染时若存在 boundary，先估"全量原文"视图水位，≤ W × PCT_L2 则忽略 boundary
        # 全量渲染（切到大档桌子宽裕就把原文都放回来，记录永不删）。
        self._context_window_tokens = window_tokens
        self._compact_pct_l2 = packing_settings.compact_pct_l2
        self._bytes_per_token = packing_settings.bytes_per_token
        self._estimate_safety = packing_settings.estimate_safety
        # L3 决策 / 动态 tail / 写手材料预算快照（v2.1 #65，§9.3 / §9.4 / §12.2）。
        self._compact_reserve_compact = packing_settings.compact_reserve_compact
        self._compact_reserve_hard = packing_settings.compact_reserve_hard
        self._compact_model = packing_settings.compact_model
        self._compact_model_window = packing_settings.compact_model_window
        self._compact_target_ratio = packing_settings.compact_target_ratio
        self._compact_material_ratio = packing_settings.compact_material_ratio
        self._compact_output_reserve = packing_settings.compact_output_reserve
        self._l3_tail_min_turns = packing_settings.l3_tail_min_turns
        # L2 粘滞开关：默认关；plan_compaction 过 L2 线后 mark_l2_packed() 置 True，
        # 此后每轮都装箱（免费、可恢复，没有理由回弹）。
        self._l2_packed = False
        # 每轮上下文片段（日期 / 记忆目录…）：只在 messages() 末尾合成，从不落盘
        self._context = list(context)
        # 序号接着已有流水往下排（不用 len：读回时可能跳过过坏行）
        self._next_seq = max((record.seq for record in self._records), default=-1) + 1
        # 上下文缓存：以「记录条数 + 是否带系统提示词 + 上下文签名 + 临时消息」为版本号。
        self._messages_cache: list[Message] | None = None
        self._messages_cache_key: tuple[object, ...] | None = None
        # 临时内部用户消息（截断续写指令等）：进了 messages() 也不清，**落账（_append）
        # 才清**，从不落盘，回放里永远看不到（见 set_transient_user_message，Bug A）。
        # 不清零点放在 messages() 里的写法会让"续写轮重发"丢指令（2026-10-01 修）。
        self._transient_user_text: str | None = None

    # ---------- 只读属性 ----------

    @property
    def session_id(self) -> str:
        """会话 id，同时也是文件名（不含目录与后缀）。"""
        return self._meta.session_id

    @property
    def session_meta(self) -> SessionMeta:
        """首行元信息（schema_version / learner_id / model / cwd / 提示词指纹…）。"""
        return self._meta

    @property
    def path(self) -> Path:
        """本会话 JSONL 文件的绝对路径。给人和排查用，调用方不要读写它。"""
        return self._store.path

    @property
    def system_prompt_mismatch(self) -> bool:
        """重开时传入的提示词与首行指纹是否不一致。

        不一致就用新传入的那份（只在系统消息里生效），这里留个标记给人排查；
        第一版不落盘、不报错（见设计稿 §附未决项 2）。
        """
        return self._system_prompt_mismatch

    @property
    def skipped_lines(self) -> int:
        """本会话读回时被跳过的行数（JSON 解析失败的行）。

        坏行只丢它自己、会话仍可续，但丢了几行必须让调用方看得到 —— 静默丢数据比当场
        报错更难排查。新建的会话恒为 0。
        """
        return self._skipped_lines

    def replay(self) -> ReplayResult:
        """重读本会话文件，给出"界面能直接画"的回放序列（**含工具结果**）。

        与 ``messages()`` 的区别：那条给模型看（重建上下文、含系统与合成兜底），
        这条给人看（保留工具输出的原始 dict、零合成）——历史会话的结构化结果
        （比如批改条目）就是靠它恢复，不用重新跑一遍工具。

        每次调用都重读文件：回放是低频动作（打开一个旧会话），不值得为此缓存
        而与"只追加流水"的状态打架。
        """
        return self._store.read_replay()

    def set_transient_user_message(self, text: str) -> None:
        """临时挂一条内部用户消息（如截断续写指令）：进 messages()、从不落盘，
        直到**任意 record_* 落账**才自动清除，回放里永远看不到（Bug A）。

        注意是"落账才清"而不是"看一眼就清"：续写轮遇上流中断重发时，主循环会再调一次
        messages() 而尚未落账，此时指令必须还在（否则重发白做）——
        见 `tests/session/test_history.py::test_transient_survives_retry_of_the_same_turn`。
        """
        self._transient_user_text = text or None
        self._messages_cache = None  # 内容变了，逼 messages() 重算

    @property
    def l2_packed(self) -> bool:
        """L2 成品装箱是否已进入**粘滞**状态（过线置位后每轮都装箱）。"""
        return self._l2_packed

    def mark_l2_packed(self) -> None:
        """把 L2 装箱置为粘滞开启（方案 §8 / §12，v2.1 #63；由 #66 的 plan_compaction
        在越过 L2 线时调用）。

        装箱是免费、纯渲染、随时可取回的减载，一旦启用没有理由回弹，所以只置位、不提供
        关闭；置位当轮废掉 messages 缓存，下一次 messages() 即按装箱重建。总开关
        ``auto_compact=False`` 时即便置位也不装箱（见 _build_messages）。
        """
        if not self._l2_packed:
            self._l2_packed = True
            self._messages_cache = None

    # ---------- 记录：调用方告诉.history 发生了什么 ----------

    async def record_user_message(self, text: str) -> None:
        """记一条用户消息：落盘一行 user_message。"""
        await self._append(UserMessagePayload(text=text))

    async def record_assistant_message(self, message: AssistantMessage) -> None:
        """记模型一轮完整输出：文本落一行 assistant_message，
        其中每个 ToolCallBlock 各落一行 tool_call（保持顺序）。
        半截消息不要调；只记已收完整的那一条。"""
        await self._append(AssistantMessagePayload(text=message.text))
        for call in message.tool_calls:
            await self._append(
                ToolCallPayload(id=call.id, name=call.name, arguments=call.arguments)
            )

    async def record_tool_result(self, call_id: str, result: ToolResult) -> None:
        """记一个工具的结果（成功/失败/被取消都记）：落一行 tool_result，
        用 call_id 与前面的 tool_call 配对。"""
        await self._append(
            ToolResultPayload(
                call_id=call_id,
                name=result.tool_name,
                ok=result.ok,
                duration_ms=result.duration_ms,
                output=result.output,
                error=result.error,
            )
        )

    async def record_turn_end(
        self,
        *,
        reason: FinishReason,
        duration_ms: float,
        usage: Usage | None = None,
        context_input_tokens: int | None = None,
        iterations: int,
    ) -> None:
        """记一次 run 的终态（为什么停 / 耗时 / 用量 / 迭代数）。"""
        await self._append(
            TurnEndPayload(
                stop_reason=reason,
                duration_ms=duration_ms,
                usage=usage,
                context_input_tokens=context_input_tokens,
                iterations=iterations,
            )
        )

    async def record_cancel(self, *, reason: str = "user", iteration: int | None = None) -> None:
        """记一次取消。取消前已开始的 tool_call 仍应各自调 record_tool_result 闭合。"""
        await self._append(CancelPayload(reason=reason, iteration=iteration))

    async def record_reasoning(self, text: str) -> None:
        """记模型的思考过程：落盘给人看，永远不进 messages()。"""
        await self._append(ReasoningPayload(text=text))

    async def record_finalize(
        self,
        *,
        grade_call_id: str,
        decisions: dict[str, str],
        final_text: str,
        overrides: dict[str, str] | None = None,
        edited_ids: list[str] | None = None,
        kept_ids: list[str] | None = None,
        comment: str = "",
    ) -> None:
        """记一条用户定稿（v2.1 #69，方案 §8.6）：落盘 finalize 行。

        定稿走独立 HTTP 端点、不经过 agent loop。它**永不进 messages()**（不是对话），
        对模型只通过提货单"已定稿"状态与 ``read_artifact(which="final")`` 暴露。
        只追加：同一篇多次定稿全部保留，读取用 :meth:`latest_finalize_for` 取最新。
        """
        await self._append(
            FinalizePayload(
                grade_call_id=grade_call_id,
                decisions=dict(decisions),
                overrides=dict(overrides or {}),
                edited_ids=list(edited_ids or []),
                kept_ids=list(kept_ids or []),
                comment=comment,
                final_text=final_text,
            )
        )

    def latest_finalize_for(self, grade_call_id: str) -> FinalizePayload | None:
        """该批改 ``grade_call_id`` 的**最新一条**定稿记录；从未定稿返回 ``None``。

        定稿只追加、不覆盖（同一篇可反复改），旧记录留在流水里可审计；锚点状态、
        ``read_artifact(which="final")`` 与 replay 一律以这里返回的最新版为准。
        """
        latest: FinalizePayload | None = None
        for record in self._records:
            if record.record_type is RecordType.FINALIZE:
                payload = record.payload_as(FinalizePayload)
                if payload.grade_call_id == grade_call_id:
                    latest = payload
        return latest

    # ---------- read_artifact：全量流水只读取回（§8.4 / §8.6，#64）----------
    def _all_grade_artifacts(self) -> list[dict[str, Any]]:
        """全量流水里每次成功的 grade_text 成品（**跨 L3 boundary**，§8.4/§9.6）。

        read_artifact 的单一真相源：成品全文只留在流水这一份（§8.5 不另建文件），
        这里只读、不搬、不复制。失败 / 非成品回报不进"保险柜"。
        """
        source_text: dict[str, str] = {}
        artifacts: list[dict[str, Any]] = []
        for record in self._records:
            rtype = record.record_type
            if rtype == RecordType.TOOL_CALL:
                call = record.payload_as(ToolCallPayload)
                if call.name == "grade_text":
                    source_text[call.id] = str(call.arguments.get("text") or "")
            elif rtype == RecordType.TOOL_RESULT:
                result = record.payload_as(ToolResultPayload)
                if result.name != "grade_text" or not result.ok:
                    continue
                output = result.output or {}
                # 成功的 GradeResult dump 必有 detections 列表；据此剔掉错误回报
                if not isinstance(output, dict) or not isinstance(
                    output.get("detections"), list
                ):
                    continue
                artifacts.append(
                    {
                        "call_id": result.call_id,
                        "source_text": source_text.get(result.call_id, ""),
                        "output": output,
                    }
                )
        return artifacts

    @staticmethod
    def _artifact_title(source_text: str) -> str:
        """提货单 / 目录一行用的篇名：原文首行前 24 字。"""
        lines = (source_text or "").strip().splitlines()
        title = lines[0].strip() if lines else ""
        return title[:24] or "（无原文的批改）"

    def artifact_catalog(self) -> dict[str, Any]:
        """保险柜全量一行目录（不传 call_id 时 read_artifact 返回它，§8.4）。"""
        items: list[dict[str, Any]] = []
        for artifact in self._all_grade_artifacts():
            cid = artifact["call_id"]
            output = artifact["output"]
            items.append(
                {
                    "call_id": cid,
                    "title": self._artifact_title(artifact["source_text"]),
                    "detection_count": len(output["detections"]),
                    "finalized": self.latest_finalize_for(cid) is not None,
                }
            )
        return {"count": len(items), "items": items}

    def read_artifact(
        self,
        call_id: str,
        *,
        item_id: str | None = None,
        which: str = "result",
    ) -> dict[str, Any] | None:
        """按代号取回成品；找不到一律返回 ``None``，由工具层转友好错误。

        - ``which="result"``（默认）：原始批改成品（附原文）；给 ``item_id`` 只取单条；
        - ``which="final"``：该篇最新定稿版（§8.6，反复定稿取最新）；无定稿返回 None。
        只读当前会话全量流水，不接受路径、不出会话。
        """
        if which == "final":
            payload = self.latest_finalize_for(call_id)
            if payload is None:
                return None
            return {
                "call_id": call_id,
                "final_text": payload.final_text,
                "decisions": payload.decisions,
                "overrides": payload.overrides,
                "edited_ids": payload.edited_ids,
                "kept_ids": payload.kept_ids,
                "comment": payload.comment,
            }

        match = next(
            (a for a in self._all_grade_artifacts() if a["call_id"] == call_id),
            None,
        )
        if match is None:
            return None
        output = match["output"]

        if item_id is not None:
            item = next(
                (
                    d
                    for d in output.get("detections", [])
                    if isinstance(d, dict) and d.get("id") == item_id
                ),
                None,
            )
            if item is None:
                return None
            return {"call_id": call_id, "item": item}

        return {
            "call_id": call_id,
            "title": self._artifact_title(match["source_text"]),
            "source_text": match["source_text"],
            "summary": output.get("summary", ""),
            "detections": output.get("detections", []),
        }

    # ---------- 收尾 ----------

    def close(self) -> None:
        """flush 并关闭文件句柄。会话结束/进程退出前调；之后不可再 record_*()。"""
        self._store.close()

    # ---------- 上下文重建 ----------

    def _context_signature(self) -> str:
        """对当前所有上下文片段的渲染结果取稳定签名；日期或记忆目录一变即变化。"""
        joined = "\u0001".join((fragment() or "") for fragment in self._context)
        return hashlib.sha1(joined.encode("utf-8")).hexdigest()

    def messages(self, *, include_system: bool = True) -> list[Message]:
        """算出「这一轮该发给模型的消息」并按顺序返回。

        返回的是一个新列表，调用方只读、不要就地改；要加东西请调 record_*()。
        系统提示词在最前（include_system=False 时不含）；tool_call 与 tool_result
        已按 call_id 配对；未闭合的 tool_call 会自动补一条错误结果，绝不悬空。
        turn_end / cancel / reasoning / compact_boundary 不出现在结果里，但 cancel
        之后若又有人提问，会补一条中断标记 assistant（见 ``SyntheticKind.INTERRUPTED_REPLY``）
        把序列接回 user → assistant → user；这条只活在本方法的返回值里，不落盘。
        """
        # 临时内部消息（截断续写指令）**在这里不清** —— 清零点只有一个：`_append`
        # 落账即清。原因：续写轮若遇上流中断要重发，主循环会再调一次 messages()
        # 而尚未落账；此时指令必须还在，否则模型不知道要"接着写"，可能把前文重写一遍，
        # 整轮重发等于白做（2026-10-01 修）。
        # ⚠️ 旧实现是"看一眼就清"，理由是"怕重发时出现两条一模一样的指令"——
        # 这个担心不成立：messages() 每次返回**新构造的列表**，transient 只会在末尾
        # 追加一条，不存在累加（cache_key 与 _build_messages 也都只放一份）。
        transient = self._transient_user_text

        # 版本键带上上下文片段的「内容签名」：片段每轮重算，既含当天日期、也含长期
        # 记忆目录。旧实现只放当天日期，同一天记忆目录被外部改动时会命中旧缓存。
        context_sig = self._context_signature() if self._context else None
        # ⚠️ 去重「换内容不换条数」，光看条数认不出它做过没有 —— 所以开关必须进版本键，
        # 否则开关变了却命中旧缓存，改了等于没改。
        # §11.3：L3 压缩状态也必须进键——最后一条 boundary 的 seq（无则 -1）标记"是否已
        # 切片、切在哪"；有效窗口 W 入键是为了 C5：切大档后"忽略 boundary 全量渲染（解压）"
        # 与切片是两份不同内容，W 一变（重开 / 运行时切档）不能命中切片视图的旧缓存。
        # 解压与否是 (boundary, 记录内容即 len, W, PCT_L2) 的确定性结果，故无需再单列标志。
        cache_key = (
            len(self._records),
            include_system,
            context_sig,
            transient,
            self._context_dedupe,
            # §11.3：L2 粘滞开关一变，发出去的消息就变，必须进缓存键，否则置位后仍命中
            # 装箱前的旧缓存，折叠等于没做。
            self._l2_packed,
            self._last_boundary_seq(),
            self._context_window_tokens,
            self._compact_pct_l2,
        )
        if self._messages_cache is not None and self._messages_cache_key == cache_key:
            return list(self._messages_cache)

        built = self._build_messages(include_system=include_system, transient=transient)
        self._messages_cache = built
        self._messages_cache_key = cache_key
        return list(built)

    def context_stats(self) -> ContextStats:
        """本轮上下文的称重结果（压缩 L0 层）：**只观测，一个字都不改**。

        它是后面所有压缩动作的尺子——去重 / 瘦身 / 摘要的阈值都从这里的真实数字来。
        调用它不会改变 ``messages()`` 的返回值（见 tests/session/test_compaction.py
        的「记账是只读的」用例）。
        """
        return measure(self.messages())

    def _build_messages(self, *, include_system: bool, transient: str | None) -> list[Message]:
        """把流水算成消息列表（§6 的算法就在这一段里）。"""
        result: list[Message] = []
        if include_system:
            result.append(SystemMessage(content=self._system_prompt))

        # 每轮上下文（日期 / 记忆目录…）紧跟系统提示词、在对话记录之前：旧实现把它
        # 无条件追加在**末尾**，末尾恰为 ToolMessage 时会形成「工具结果→user」的脆弱序列。
        # 它只活在本返回值里、不落盘（render_context_message 契约），片段全空则不追加。
        context_message = render_context_message(self._context)
        if context_message is not None:
            result.append(context_message)

        # L3（§11.1）：最后一条 boundary 决定"续接摘要 user + body 只取切点之后 tail"。
        # C5：切到更大档位、全量原文其实放得下（≤ L2 线）时忽略 boundary 全量渲染，
        # 记录永不删。summary / boundary 记录本身在下面循环里按元记录整类跳过。
        boundary = self._last_compact_boundary()
        resume_injected = False
        body_records = self._records
        if boundary is not None and not self._should_decompress(boundary):
            body_records = [r for r in self._records if r.seq > boundary.at_seq]
            # 续接 user = 固定话术 + trust 袋子包最后摘要 + 全量流水重扫的提货单（§9.6）。
            # C4 实测 chat 通道允许相邻 user，故与 context 注入各一条、不合并。
            result.append(
                UserMessage(
                    content=render_resume_user(
                        self._summary_text_for(boundary),
                        picklist_text=self._render_full_picklist(boundary.at_seq),
                    )
                )
            )
            resume_injected = True

        # 事实流水 → 消息序列（工具配对 / 取消中断合成等兜底）；summary / boundary 等
        # 元记录在 _synthesize 内整类跳过，切片时 body 已只含切点之后的 tail。
        result.extend(self._synthesize(body_records))

        # 临时内部用户消息（截断续写指令）：只活在本次上下文、从不落盘；
        # 放在对话记录之后（续写时它要跟在半截 assistant 后）；context 已在最前。
        if transient:
            result.append(UserMessage(content=transient))

        # 压缩 L1：同一份材料只留最近一份（复核会把整篇原文反复重发，一篇最多 5 次）。
        # 放在最后一步：去重只换字段内容、不改条数顺序，所以上面所有配对 / 闭合 /
        # 补中断标记的逻辑照旧成立。
        if self._context_dedupe:
            result = dedupe_safely(result)

        # 压缩 L2：粘滞置位且总开关开着时，把已关闭、出了最近 K 轮窗口的批改成品折成
        # 提货单（免费、纯渲染、可 read_artifact 取回）。排在 L1 之后：两者都不改条数
        # 顺序（L2 只把 tool 结果换成占位、另插一条 system 提货单），配对依旧合法。
        if self._l2_packed and self._auto_compact:
            # 已注入 L3 续接 user（内含一份全量提货单）时，装箱只折叠、不重复插提货单。
            result = self._apply_l2_packing(
                result, inject_picklist=not resume_injected
            )

        return result

    def _synthesize(self, records: Sequence[Record]) -> list[Message]:
        """把一段流水记录合成为可发给模型的消息（不含 system / context / transient / L1 / L2）。

        负责工具配对（悬空 tool_call 补 MISSING_TOOL_RESULT）、cancel 之后补
        INTERRUPTED_REPLY 把角色接回 user→assistant→user；session_meta / turn_end /
        reasoning / compact_summary / compact_boundary / finalize / 未知类型整类跳过。
        L3 切片后只传 tail 记录，续接摘要由 :meth:`_build_messages` 另插一条 user（§11.1）。
        """
        result: list[Message] = []
        open_assistant: AssistantMessage | None = None
        unanswered_ids: list[str] = []
        pending_cancel = False  # 刚被取消：下一条 user 之前要补一条中断标记
        seen_user = False  # 前面已经有人说过了（流水万一以 cancel 开头就不补）

        def close_open_assistant() -> None:
            """给没等到结果的 tool_call 各补一条错误结果 —— 悬空会让千问报 400。"""
            nonlocal open_assistant, unanswered_ids
            if open_assistant is None:
                return
            for call in open_assistant.tool_calls:
                if call.id in unanswered_ids:
                    result.append(
                        ToolMessage(
                            tool_call_id=call.id,
                            name=call.name,
                            content=SYNTHETIC_TEXTS[SyntheticKind.MISSING_TOOL_RESULT],
                            is_error=True,
                        )
                    )
            open_assistant = None
            unanswered_ids = []

        for record in records:
            match record.record_type:
                case RecordType.USER_MESSAGE:
                    close_open_assistant()
                    if pending_cancel and seen_user:
                        result.append(
                            AssistantMessage(
                                blocks=[
                                    TextBlock(text=SYNTHETIC_TEXTS[SyntheticKind.INTERRUPTED_REPLY])
                                ]
                            )
                        )
                    pending_cancel = False
                    seen_user = True
                    result.append(UserMessage(content=record.payload_as(UserMessagePayload).text))
                case RecordType.CANCEL:
                    # 取消本身不进上下文，但记住「这里断过一次」，后面真有人提问时补中断
                    # 标记。刻意不在这里闭合 assistant：工具可能取消后才回报，提前补兜底会
                    # 和真结果撞成两条同 call_id 的 ToolMessage；闭合推迟到下一个 user / 扫完。
                    pending_cancel = True
                case RecordType.ASSISTANT_MESSAGE:
                    close_open_assistant()
                    pending_cancel = False  # 模型确实回过话了，不用再补标记
                    text = record.payload_as(AssistantMessagePayload).text
                    open_assistant = AssistantMessage(blocks=[TextBlock(text=text)] if text else [])
                    unanswered_ids = []
                    result.append(open_assistant)
                case RecordType.TOOL_CALL:
                    call = record.payload_as(ToolCallPayload)
                    if open_assistant is None:
                        # 契约上不会发生（tool_call 只能跟在 assistant_message 之后）。
                        # 真在旧文件里读到，就补一个空壳把调用接住，而不是悄悄扔掉。
                        open_assistant = AssistantMessage(blocks=[])
                        unanswered_ids = []
                        result.append(open_assistant)
                    open_assistant.blocks.append(
                        ToolCallBlock(id=call.id, name=call.name, arguments=call.arguments)
                    )
                    unanswered_ids.append(call.id)
                case RecordType.TOOL_RESULT:
                    tool_result = record.payload_as(ToolResultPayload)
                    result.append(
                        ToolMessage(
                            tool_call_id=tool_result.call_id,
                            name=tool_result.name,
                            content=_tool_result_content(tool_result),
                            is_error=not tool_result.ok,
                        )
                    )
                    if tool_result.call_id in unanswered_ids:
                        unanswered_ids.remove(tool_result.call_id)
                case _:
                    # session_meta / turn_end / reasoning / compact_summary /
                    # compact_boundary / finalize / 未知类型：不是发给模型的对话，整类跳过。
                    continue
        close_open_assistant()
        return result

    def _apply_l2_packing(
        self, messages: list[Message], *, inject_picklist: bool = True
    ) -> list[Message]:
        """对已重建消息做一次 L2 成品装箱（方案 §8）。**只读流水、纯渲染**。

        §9.6：锚点清单**每次都从全量流水重扫**（跨 L3 boundary），保证压多少次历史上每篇
        成品都还在提货单上、都能 read_artifact 取回。tail body 里已不存在的旧 call_id 在
        messages 中找不到、自然不折叠，无害。``inject_picklist=False`` 用于已注入 L3 续接
        user（其自带一份全量提货单）时，只折叠、不重复插单。
        """
        tasks = scan_grade_tasks(
            self._records,
            task_idle_turns=self._task_idle_turns,
            keep_recent_turns=self._fold_keep_recent,
        )
        packed = pack_artifacts(
            messages,
            tasks,
            detail_recent=self._anchor_detail_recent,
            budget_bytes=self._l2_budget_bytes,
            inject_picklist=inject_picklist,
        )
        packed_messages = packed.messages
        # §8.4 B3：read_artifact 取回结果归成品桶——同目标去重、过 K 轮折回一行。
        # 与装箱同一次渲染里完成（取回只可能发生在 L2 启用、提货单引导之后）。
        packed_messages, _ = fold_artifact_reads(
            packed_messages,
            self._records,
            keep_recent_turns=self._artifact_result_keep_recent,
        )
        return packed_messages

    # ---------- L3：boundary / 摘要 / 续接（方案 §9.5 / §9.6 / §11.1，v2.1 #65） ----------

    def _last_compact_boundary(self) -> CompactBoundaryPayload | None:
        """最后一条压缩边界；从未压缩返回 None。多次压缩永远只认最后一条（§9.6）。"""
        for record in reversed(self._records):
            if record.record_type is RecordType.COMPACT_BOUNDARY:
                return record.payload_as(CompactBoundaryPayload)
        return None

    def _last_boundary_seq(self) -> int:
        """最后一条 boundary 记录的 seq（无则 -1），供 messages 缓存键区分切片状态。"""
        for record in reversed(self._records):
            if record.record_type is RecordType.COMPACT_BOUNDARY:
                return record.seq
        return -1

    def _summary_text_for(self, boundary: CompactBoundaryPayload) -> str:
        """续接要渲染的摘要正文：优先 boundary 指针指向的那条，退回最后一条摘要。

        都找不到（理论不该发生：boundary 必先配一条摘要）给一句兜底话术，绝不炸渲染。
        """
        for record in self._records:
            if (
                record.seq == boundary.summary_record_seq
                and record.record_type is RecordType.COMPACT_SUMMARY
            ):
                return record.payload_as(SummaryPayload).text
        for record in reversed(self._records):
            if record.record_type is RecordType.COMPACT_SUMMARY:
                return record.payload_as(SummaryPayload).text
        return (
            "（更早的对话已浓缩，但摘要记录暂时读不到；需要某篇细节时请用 "
            "read_artifact 按编号调阅。）"
        )

    def _render_full_picklist(self, at_seq: int) -> str | None:
        """从**全量流水**（跨 boundary）扫描成品、按 §8.3 分级与字节预算渲染提货单（§9.6）。

        切点 ``at_seq`` 之前的成品已被切在 tail 之外，哪怕状态机仍判它"开放 / 在最近窗"
        也强制列上（``force_include``），保证被切走的每一篇都有 read_artifact 取回锚点；
        tail 内（grade_seq 更新）的成品正文还在 body，按 L2 常规规则决定是否列。
        """
        tasks = scan_grade_tasks(
            self._records,
            task_idle_turns=self._task_idle_turns,
            keep_recent_turns=self._fold_keep_recent,
        )
        forced = frozenset(t.call_id for t in tasks if t.grade_seq <= at_seq)
        return render_picklist(
            tasks,
            detail_recent=self._anchor_detail_recent,
            budget_bytes=self._l2_budget_bytes,
            force_include=forced,
        )

    def _should_decompress(self, boundary: CompactBoundaryPayload) -> bool:
        """C5：存在 boundary 时，若"忽略全部 boundary、全量原文"视图仍 ≤ L2 线，则解压。

        切到大档（桌子宽裕）就把原文都放回模型眼前；boundary / summary 记录始终保留在
        流水里，切回小档或继续增长后切片自动恢复。判定用 L1 后、L2 前的全量视图估算
        （偏保守：宁可不解压也不撑超）。任何意外都退回"继续切片"（更小、更安全）。
        """
        try:
            full: list[Message] = [SystemMessage(content=self._system_prompt)]
            context_message = render_context_message(self._context)
            if context_message is not None:
                full.append(context_message)
            full.extend(self._synthesize(self._records))
            if self._context_dedupe:
                full = dedupe_safely(full)
            estimated = estimate_tokens_from_bytes(
                measure(full).total_bytes,
                bytes_per_token=self._bytes_per_token,
                safety=self._estimate_safety,
            )
            l2_line = int(self._context_window_tokens * self._compact_pct_l2)
            return estimated <= l2_line
        except BaseException:
            _LOGGER.warning("C5 解压判定失败，本轮继续按 boundary 切片", exc_info=True)
            return False

    def _records_after_last_boundary(self, records: list[Record]) -> list[Record]:
        """返回最后一条 compact_boundary 切点之后的记录（§9.6：永远只认最后一条）。

        从未压缩时原样返回全部。切点之后仍包含 summary / boundary 自身（它们 seq 更大），
        但这两类在 :meth:`_synthesize` 里按元记录整类跳过，不会变成消息。plan_compaction
        用它圈定"自上次压缩以来的增量工作区"。
        """
        at_seq: int | None = None
        for record in records:
            if record.record_type is RecordType.COMPACT_BOUNDARY:
                at_seq = record.payload_as(CompactBoundaryPayload).at_seq
        if at_seq is None:
            return list(records)
        return [record for record in records if record.seq > at_seq]

    # ---------- L3：plan / commit（方案 §9.3 / §9.4 / §9.6 / §12.2 / §12.4） ----------

    def plan_compaction(
        self,
        *,
        effective_window: int,
        last_input_tokens: int | None,
        last_output_tokens: int | None,
        estimated_tokens: int | None = None,
        new_user_estimated_tokens: int = 0,
    ) -> CompactionPlan:
        """本轮发送前的**纯本地**压缩决策（不碰网络、不改流水，§12.2）。

        一圈：判级 → 过 L2 线就翻转 L2 粘滞 → 过 L3 线则选动态 tail、装写手材料
        （二次压缩带上一条摘要做种子）。"一轮最多一次 L3"由 loop（#66）保证，本方法
        自身无状态、可重复调用而不落任何副作用（除 L2 粘滞翻转幂等）。

        ``estimated_tokens`` 为 None（runtime 层的 loop 拿不到 session 称重函数）时，
        自行 measure 当前消息、按字节保守换算兜底；有真实 usage 时决策以真实水位为准。
        """
        if estimated_tokens is None:
            estimated_tokens = estimate_tokens_from_bytes(
                measure(self.messages(include_system=True)).total_bytes,
                bytes_per_token=self._bytes_per_token,
                safety=self._estimate_safety,
            )
        decision = decide_compaction(
            effective_window=effective_window,
            last_input_tokens=last_input_tokens,
            last_output_tokens=last_output_tokens,
            estimated_tokens=estimated_tokens,
            new_user_estimated_tokens=new_user_estimated_tokens,
            l2_active=self._l2_packed,
            auto_compact=self._auto_compact,
            pct_l2=self._compact_pct_l2,
            reserve_compact=self._compact_reserve_compact,
            reserve_hard=self._compact_reserve_hard,
        )
        if "l2" in decision.levels:
            self.mark_l2_packed()
        if decision.state != "compact" or "l3" not in decision.levels:
            return CompactionPlan(
                state=decision.state,
                levels=decision.levels,
                predicted_tokens=decision.predicted_tokens,
                window_tokens=decision.window_tokens,
                reason=decision.reason,
            )
        return self._build_l3_plan(decision)

    def _build_l3_plan(self, decision) -> CompactionPlan:
        """过 L3 线后：在工作区上切动态 tail、装写手材料、定切点与 boundary_id。"""
        boundary = self._last_compact_boundary()
        # 工作区 = 最后 boundary 之后（首次压缩为全量），并剔除 summary / boundary 元记录，
        # 只对真实对话往返切 turn（§9.6：二次压缩的材料是"上条摘要 + 增量"）。
        work = self._records_after_last_boundary(self._records)
        meta_types = (RecordType.COMPACT_SUMMARY, RecordType.COMPACT_BOUNDARY)
        conv = [record for record in work if record.record_type not in meta_types]
        turns = split_turns(conv)

        stats = measure(self.messages(include_system=True))
        # tools schema 不落在 messages 里；其约 3–4k token 由 target 0.6 的余量与估算
        # safety 覆盖（#17 C2 实测 RESERVE_HARD=12k 已够），这里不另加常数。
        fixed_overhead = stats.system_bytes + stats.context_bytes
        tail_budget = tail_budget_bytes(
            window_tokens=decision.window_tokens,
            target_ratio=self._compact_target_ratio,
            fixed_overhead_bytes=fixed_overhead,
            output_reserve_tokens=self._compact_output_reserve,
            picklist_budget_bytes=self._l2_budget_bytes,
            bytes_per_token=self._bytes_per_token,
        )
        tail = select_tail(
            turns, budget_bytes=tail_budget, min_turns=self._l3_tail_min_turns
        )

        material_source = [record for record in conv if record.seq <= tail.at_seq]
        material = select_material(
            split_turns(material_source),
            budget_bytes=material_budget_bytes(
                self._compact_model_window,
                self._compact_material_ratio,
                self._bytes_per_token,
            ),
        )

        seed_seq: int | None = None
        seed_text: str | None = None
        if boundary is not None:
            seed_seq = boundary.summary_record_seq
            seed_text = self._summary_text_for(boundary)

        return CompactionPlan(
            state=decision.state,
            levels=decision.levels,
            predicted_tokens=decision.predicted_tokens,
            window_tokens=decision.window_tokens,
            reason=decision.reason,
            at_seq=tail.at_seq,
            boundary_id=f"c-{uuid.uuid4().hex[:12]}",
            tail_records=tuple(tail.tail_records),
            tail_turns=tail.tail_turns,
            tail_over_target=tail.over_target_at_min,
            material_records=tuple(material.material_records),
            material_turn_count=material.turn_count,
            material_truncated=material.truncated,
            seed_summary_seq=seed_seq,
            seed_summary_text=seed_text,
            composition=composition_tokens(
                stats,
                bytes_per_token=self._bytes_per_token,
                safety=self._estimate_safety,
            ),
        )

    async def commit_compaction(
        self, plan: CompactionPlan, summary_text: str, *, model: str | None = None
    ) -> CompactionReport:
        """写手成功后落账：先摘要、后边界（两条顺序追加），随后 messages() 即按新切点切片。

        任何非取消异常都**不炸主循环**（§12.4）：捕获后返回 ``success=False`` 的降级报告，
        loop 本轮照常全量发送；``CancelledError`` 单独向上抛（取消语义不能被吞）。
        """
        before = plan.predicted_tokens
        if (
            plan.state != "compact"
            or "l3" not in plan.levels
            or plan.at_seq is None
            or not plan.boundary_id
        ):
            return CompactionReport(
                success=False,
                level="",
                boundary_id="",
                at_seq=None,
                summary_record_seq=None,
                before_tokens=before,
                after_tokens=None,
                degraded_reason="plan 未要求 L3 压缩，跳过落账",
            )
        try:
            await self._append(
                SummaryPayload(
                    text=summary_text,
                    covered_up_to_seq=plan.at_seq,
                    material_truncated=plan.material_truncated,
                    model=model or self._compact_model,
                )
            )
            summary_seq = self._records[-1].seq
            await self._append(
                CompactBoundaryPayload(
                    boundary_id=plan.boundary_id,
                    at_seq=plan.at_seq,
                    summary_record_seq=summary_seq,
                )
            )
            self.mark_l2_packed()
            after = estimate_tokens_from_bytes(
                measure(self.messages(include_system=True)).total_bytes,
                bytes_per_token=self._bytes_per_token,
                safety=self._estimate_safety,
            )
            return CompactionReport(
                success=True,
                level="l3",
                boundary_id=plan.boundary_id,
                at_seq=plan.at_seq,
                summary_record_seq=summary_seq,
                before_tokens=before,
                after_tokens=after,
                material_truncated=plan.material_truncated,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # 见 docstring：任何意外都降级、不炸主循环
            _LOGGER.warning("L3 commit 落账失败，本轮不压缩、全量继续", exc_info=True)
            return CompactionReport(
                success=False,
                level="l3",
                boundary_id=plan.boundary_id,
                at_seq=plan.at_seq,
                summary_record_seq=None,
                before_tokens=before,
                after_tokens=None,
                material_truncated=plan.material_truncated,
                degraded_reason=f"commit 落账失败：{exc}",
            )

    # ---------- §10 413 应急：无写手、不落盘的临时减重视图（B5 / C3） ----------

    def emergency_view(
        self, *, effective_window: int, strength: int = 0
    ) -> EmergencyView:
        """§10 应急减重渲染（**纯本地、不调写手、不改流水**），供 loop 减重后重发本轮。

        - 只保留最近 N 个完整往返：``strength=0`` 保 ``l3_tail_min_turns`` 个（默认 2），
          ``strength=1`` 退到下限 1 个（loop 最多再缩这一次，绝不退过 1 往返）；
        - 续接 user 带上**已有**摘要（若之前压过）+ 全量提货单，没有摘要也不伪造；
        - **B5**：未关闭 grade 任务、且被切在 tail 之外的用户来稿原文，逐条用 trust 袋子
          强制保留（应急永不删未结束任务的 user 原文）；
        - 降到硬线（``W - RESERVE_HARD``）以下才算可行；``strength=1`` 仍不可行时，
          ``feasible=False`` 并在 ``reason`` 给人话出路（区分 B5 单篇超长 / 整体过长）。
        """
        try:
            return self._emergency_view(effective_window=effective_window, strength=strength)
        except BaseException:  # 应急本身坏了也不能卡死：明确失败、由 loop 给人话
            _LOGGER.warning("§10 应急减重渲染失败，转明确失败", exc_info=True)
            return EmergencyView(
                feasible=False,
                messages=[],
                estimated_tokens=0,
                kept_turns=0,
                protected_user_texts=(),
                strength=strength,
                reason=_EMERGENCY_OPEN_NEW_SESSION,
            )

    def _emergency_view(self, *, effective_window: int, strength: int) -> EmergencyView:
        boundary = self._last_compact_boundary()
        work = self._records_after_last_boundary(self._records)
        meta_types = (RecordType.COMPACT_SUMMARY, RecordType.COMPACT_BOUNDARY)
        conv = [r for r in work if r.record_type not in meta_types]
        turns = split_turns(conv)

        n_keep = max(1, int(self._l3_tail_min_turns) - max(0, int(strength)))
        kept_turns = turns[-n_keep:] if turns else []
        tail_records = [record for turn in kept_turns for record in turn.records]
        tail_seq_set = {record.seq for record in tail_records}
        tail_max_seq = max(tail_seq_set, default=-1)

        protected = self._protected_open_grade_texts(tail_seq_set)
        picklist = self._render_full_picklist(tail_max_seq)
        summary_text = (
            self._summary_text_for(boundary) if boundary is not None else None
        )
        resume = render_emergency_resume(
            summary_text=summary_text,
            picklist_text=picklist,
            protected_user_texts=protected,
        )

        built: list[Message] = [SystemMessage(content=self._system_prompt)]
        context_message = render_context_message(self._context)
        if context_message is not None:
            built.append(context_message)
        built.append(UserMessage(content=resume))
        built.extend(self._synthesize(tail_records))
        if self._transient_user_text:
            built.append(UserMessage(content=self._transient_user_text))
        if self._context_dedupe:
            built = dedupe_safely(built)
        # 应急不再跑 L2 装箱：全量提货单已在续接 user 里，再折叠只会重复、不省更多。

        estimated = estimate_tokens_from_bytes(
            measure(built).total_bytes,
            bytes_per_token=self._bytes_per_token,
            safety=self._estimate_safety,
        )
        hard_limit = effective_window - self._compact_reserve_hard
        if estimated <= hard_limit:
            return EmergencyView(
                feasible=True,
                messages=built,
                estimated_tokens=estimated,
                kept_turns=len(kept_turns),
                protected_user_texts=tuple(protected),
                strength=strength,
            )

        # 已退到下限 1 往返仍超：归因后给人话。去掉 B5 保护原文若就能 fit，说明是单篇
        # 来稿撑爆（B5 不许砍）→ 给"存文件 / 分两篇"；否则是整体过长 → 建议新开会话。
        reason = _EMERGENCY_OPEN_NEW_SESSION
        if protected:
            no_protected = render_emergency_resume(
                summary_text=summary_text,
                picklist_text=picklist,
                protected_user_texts=(),
            )
            base_msgs: list[Message] = [SystemMessage(content=self._system_prompt)]
            if context_message is not None:
                base_msgs.append(context_message)
            base_msgs.append(UserMessage(content=no_protected))
            base_msgs.extend(self._synthesize(tail_records))
            if self._transient_user_text:
                base_msgs.append(UserMessage(content=self._transient_user_text))
            if self._context_dedupe:
                base_msgs = dedupe_safely(base_msgs)
            base_est = estimate_tokens_from_bytes(
                measure(base_msgs).total_bytes,
                bytes_per_token=self._bytes_per_token,
                safety=self._estimate_safety,
            )
            if base_est <= hard_limit:
                reason = _EMERGENCY_SINGLE_USER_TOO_LONG

        return EmergencyView(
            feasible=False,
            messages=built,
            estimated_tokens=estimated,
            kept_turns=len(kept_turns),
            protected_user_texts=tuple(protected),
            strength=strength,
            reason=reason,
        )

    def _protected_open_grade_texts(self, tail_seq_set: set[int]) -> list[str]:
        """B5：收集未关闭 grade 任务里、被应急 tail 切在外面的用户来稿原文（去重保序）。"""
        tasks = scan_grade_tasks(
            self._records,
            task_idle_turns=self._task_idle_turns,
            keep_recent_turns=self._fold_keep_recent,
        )
        by_seq = {record.seq: record for record in self._records}
        protected: list[str] = []
        seen: set[str] = set()
        for task in tasks:
            if task.closed or task.grade_seq in tail_seq_set:
                continue
            record = by_seq.get(task.grade_seq)
            if record is None:
                continue
            text = str(record.payload_as(ToolCallPayload).arguments.get("text") or "").strip()
            if text and text not in seen:
                seen.add(text)
                protected.append(text)
        return protected

    # ---------- 内部：写一条记录 ----------

    async def _append(self, payload: RecordPayload) -> None:
        """落盘一行 + 记进内存列表（两者共用同一份真相）。"""
        record = build_record(seq=self._next_seq, ts=self._clock().isoformat(), payload=payload)
        await self._store.append(record)
        self._records.append(record)
        self._next_seq += 1
        # 临时内部消息只作用于"落账前那一轮"，一旦有新记录入账即清，不残留（Bug A）
        if self._transient_user_text is not None:
            self._transient_user_text = None
            self._messages_cache = None


def _resolve_dedupe(value: bool | None) -> bool:
    """去重开关：显式传了就用传的，没传去配置单点读（R1：环境变量可关）。"""
    if value is not None:
        return value
    return load_settings().context_dedupe


async def new_session(
    *,
    system_prompt: str,
    model: str,
    cwd: str,
    root: Path | None = None,
    clock: Callable[[], datetime] = local_now,
    context: Sequence[ContextFragment] = (),
    context_dedupe: bool | None = None,
) -> SessionHistory:
    """开一个新会话：算路径、建文件、写首行 session_meta，返回 history 对象。

    提示词本身不落盘，只在 meta 里留指纹；重开时由调用方重新传入。
    ``context`` 是每轮上下文片段（日期 / 记忆目录…），只在 messages() 合成、不落盘。
    ``context_dedupe`` 为 None 时从配置单点读（``GLYPH_CONTEXT_DEDUPE``）。
    """
    now = clock()
    session_id = generate_session_id(now)
    path = session_path(session_id, now, root=root)
    meta = SessionMeta(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        # learner_id 第一版恒 None：预留给本地匿名学习者档案，路径里不含它（D17）
        learner_id=None,
        model=model,
        cwd=cwd,
        created_at=now.isoformat(),
        app_version=APP_VERSION,
        system_prompt_fingerprint=system_prompt_fingerprint(system_prompt),
    )
    history = SessionHistory(
        store=SessionStore(path, clock=clock),
        meta=meta,
        system_prompt=system_prompt,
        clock=clock,
        context=context,
        context_dedupe=_resolve_dedupe(context_dedupe),
    )
    await history._append(meta)
    return history


async def open_session(
    session_id: str,
    *,
    system_prompt: str,
    root: Path | None = None,
    clock: Callable[[], datetime] = local_now,
    context: Sequence[ContextFragment] = (),
    context_dedupe: bool | None = None,
) -> SessionHistory:
    """按 id 打开已有会话：读回流水重建内存态，返回 history 对象。

    文件不存在 → ``SessionNotFoundError``；首行不是 session_meta → ``SessionCorruptError``。
    提示词指纹对不上 → 用新传入的那份，并在内存里记一笔 ``system_prompt_mismatch``。
    ``context`` 片段由当前装配根提供（换启用 / 停用记忆时随之变化），不写进流水。
    """
    path = session_path(session_id, root=root)
    if not path.exists():
        raise SessionNotFoundError(f"会话不存在：{session_id}（{path}）")

    store = SessionStore(path, clock=clock)
    read = store.read_records()  # 缺首行 meta 时这里抛 SessionCorruptError
    meta = read.records[0].payload_as(SessionMeta)
    mismatch = meta.system_prompt_fingerprint != system_prompt_fingerprint(system_prompt)

    return SessionHistory(
        store=store,
        meta=meta,
        system_prompt=system_prompt,
        records=read.records,
        system_prompt_mismatch=mismatch,
        skipped_lines=read.skipped_lines,  # 跳过了几行坏数据要一路透给调用方
        clock=clock,
        context=context,
        context_dedupe=_resolve_dedupe(context_dedupe),
    )
