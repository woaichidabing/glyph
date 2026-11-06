"""把会话流水回灌成"界面能直接画"的回放序列。

**为什么要有这一层**：界面重开一个历史会话时，如果只拿 user / assistant 的文本，
**结构化批改结果（工具返回的 output）会整个丢掉**——使用者点开旧会话只看得见 AI 说过的话，
看不见它批出来的条目，要看就得重新跑一遍（那是要花钱的）。

工具结果本来就在流水里（``ToolResultPayload.output`` 存着工具的完整输出），
只是没人把它回灌出来。这一层负责把"流水"翻成"回放"，别的都不管。

**边界**：只读视图，不写盘、不改流水、**不认识任何具体工具的业务含义**
（它不知道 grade_text 是什么，只知道"某个工具返回了一个 output"）。

**严格度与 ``history.messages()`` 一致**：都走 ``Record.payload_as()`` 严格校验，
坏载荷**只丢它自己那一条**，不让一条脏数据把整个会话读崩（错误即数据）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..context import is_internal_user_message
from ..types import Usage
from .records import (
    AssistantMessagePayload,
    FinalizePayload,
    ReasoningPayload,
    Record,
    RecordType,
    ToolCallPayload,
    ToolResultPayload,
    TurnEndPayload,
    UserMessagePayload,
)

__all__ = ["ReplayItem", "ReplayResult", "build_replay", "extract_session_usage"]


class ReplayItem(BaseModel):
    """回放里的一条。五类，界面按 ``kind`` 分着画。

    - ``user`` / ``assistant``：只有 ``text`` 有用；
    - ``reasoning``：模型当时的思考过程（阶段 2 思考采集）。只给界面折成
      「思考块」展示；**不进模型上下文**，回放里也只是它自己一条，不混进答复文本。
    - ``tool``：工具跑完的回报。``output`` 是工具原样的输出 dict
      （工具返回的逐条结果就在里面），``arguments`` 是当时的入参
      （里面有被处理时的原文），失败时 ``ok=False`` 且 ``error`` 是人话原因。
    - ``status``：给界面看的回合终态；取消回合即使没有完整 assistant 消息也必须可见。
    - ``finalize``：用户对某次批改的定稿区块（v2.1 #69），``call_id`` 即 grade_call_id，
      ``final_text`` 是改后全文，``text`` 复用为评语；同一篇多次定稿只回灌最新一条。

    ``seq`` / ``ts`` 五类都有：界面要靠它排序、跳回当时那条消息。
    """

    kind: Literal["user", "assistant", "reasoning", "tool", "status", "finalize"]
    seq: int = 0
    ts: str = ""
    text: str = ""
    # 以下只有 tool 用得上
    tool_name: str = ""
    call_id: str = ""
    # 工具**被宣告**时的流水序号（不是结果落盘时的 seq）。并行工具谁先跑完谁先落盘，
    # 只看结果 seq 会把后调用的排到前面（与实时看到的先后相反），界面要按这个号排序。
    # 找不到对应调用时为 None，界面退回用 seq。
    call_seq: int | None = None
    ok: bool = True
    output: dict[str, Any] | None = None
    error: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float = 0.0
    # status 条目使用：当前只展示 cancelled；finish_reason 保留终态原因。
    status: str | None = None
    finish_reason: str | None = None
    # 以下只有 finalize 条目用得上（v2.1 #69，方案 §8.6）
    final_text: str = ""
    decisions: dict[str, str] = Field(default_factory=dict)
    overrides: dict[str, str] = Field(default_factory=dict)
    edited_ids: list[str] = Field(default_factory=list)
    kept_ids: list[str] = Field(default_factory=list)


@dataclass
class ReplayResult:
    """回放结果：条目 + **被跳过的行数** + 最后一轮的真实用量。

    跳过多少一定要告诉调用方——回放是给使用者看历史的通道，少两条他看不出来，
    静默丢数据比报错更难排查。``last_input_tokens`` 是会话最后一轮模型回报的
    上下文 token 数，界面切回历史会话时用量环靠它回显；没有任何一轮带 usage 时为 None。
    """

    items: list[ReplayItem] = field(default_factory=list)
    skipped_lines: int = 0
    last_input_tokens: int | None = None
    # 本会话逐轮相加的用量（状态条回显用）。语义与 last_input_tokens 不同：那条是
    # "最后一轮上下文多大"（圆环），这条是"一共花了多少、其中多少重复算"（省了多少）。
    # 老会话 / 模型从没回用量时为 None —— 界面据此整行不显示，不显示假的 0。
    usage: Usage | None = None


def build_replay(records: Iterable[Record]) -> list[ReplayItem]:
    """把一段流水翻成回放序列，**顺序与流水一致**。

    认 user / assistant / reasoning / tool，以及取消对应的 status；其余
    （session_meta / compact_boundary）整条跳过——它们不是"给用户看的内容"。
    不在词典里的 ``type`` 也跳过（``record_type`` 返回 None）。

    - **入参先固化成 list**：调用方可能给一次性生成器，而下面要边扫边配对这个序列；
    - **单趟**：工具调用一定排在它的结果之前（写入契约如此），所以边扫边记调用就够；
    - **工具结果会回带它对应调用的工具名与入参**：界面重建结果面板需要"当时处理的是哪篇原文"，
      而原文只在 ``tool_call`` 的 arguments 里，不在结果里；
    - **坏载荷只丢它自己**：任何一条解析不过就跳过这条，不让它把整个会话读崩。
    """
    items: list[ReplayItem] = []
    # 值 = (调用载荷, 该调用所在的流水序号)：结果要回带"当时是第几个被叫起来的"
    calls: dict[str, tuple[ToolCallPayload, int]] = {}
    cancelled_since_user = False

    records_list = list(records)
    # 同一篇（grade_call_id）多次定稿只回灌最新一条：先算出每个 id 最新定稿所在 seq，
    # 主循环里旧版定稿整条跳过（旧记录仍在流水里可审计，只是不进用户视图）。
    latest_finalize_seq: dict[str, int] = {}
    for rec in records_list:
        if rec.record_type is RecordType.FINALIZE:
            try:
                payload = rec.payload_as(FinalizePayload)
            except ValidationError:
                continue
            latest_finalize_seq[payload.grade_call_id] = rec.seq

    for rec in records_list:
        rt = rec.record_type
        try:
            if rt == RecordType.USER_MESSAGE:
                payload = rec.payload_as(UserMessagePayload)
                # 内部指令（截断续写等）进账本、给模型看，但不是用户说的话，不进用户回放
                if is_internal_user_message(payload.text):
                    continue
                cancelled_since_user = False
                items.append(ReplayItem(kind="user", seq=rec.seq, ts=rec.ts, text=payload.text))
            elif rt == RecordType.ASSISTANT_MESSAGE:
                payload = rec.payload_as(AssistantMessagePayload)
                items.append(
                    ReplayItem(kind="assistant", seq=rec.seq, ts=rec.ts, text=payload.text)
                )
            elif rt == RecordType.REASONING:
                # 思考过程：只回灌成界面上的「思考块」；它永远不是给模型的上下文，
                # 所以只是回放里自己一条，绝不并进 assistant 文本（阶段 2 思考采集）
                payload = rec.payload_as(ReasoningPayload)
                items.append(
                    ReplayItem(kind="reasoning", seq=rec.seq, ts=rec.ts, text=payload.text)
                )
            elif rt == RecordType.TOOL_CALL:
                payload = rec.payload_as(ToolCallPayload)
                if payload.id:
                    calls[payload.id] = (payload, rec.seq)
            elif rt == RecordType.TOOL_RESULT:
                payload = rec.payload_as(ToolResultPayload)
                entry = calls.get(payload.call_id)
                call = entry[0] if entry else None
                items.append(
                    ReplayItem(
                        kind="tool",
                        seq=rec.seq,
                        ts=rec.ts,
                        tool_name=payload.name,
                        call_id=payload.call_id,
                        call_seq=entry[1] if entry else None,
                        ok=payload.ok,
                        output=payload.output,
                        error=payload.error,
                        arguments=call.arguments if call else {},
                        duration_ms=payload.duration_ms,
                    )
                )
            elif rt == RecordType.CANCEL:
                # 取消是用户可见的回合终态。不要回灌半截 assistant 原文，
                # 只保留事实，避免作文材料借历史回放形成提示注入通道。
                items.append(
                    ReplayItem(
                        kind="status",
                        seq=rec.seq,
                        ts=rec.ts,
                        text="这一轮已手动停止",
                        status="cancelled",
                        finish_reason="cancelled",
                    )
                )
                cancelled_since_user = True
            elif rt == RecordType.TURN_END:
                # 正常完成不需要额外 status；若只有 turn_end 而缺 cancel 记录，
                # 仍要让历史界面显示 cancelled，防止写盘顺序异常时静默丢状态。
                reason = rec.payload.get("stop_reason")
                if reason == "failed":
                    # 循环级失败（工具连续失败到阈值等）同样是回合终态：
                    # 不写出来，界面只会看到一轮没有内容的空回复，人不知道它失败了。
                    items.append(
                        ReplayItem(
                            kind="status",
                            seq=rec.seq,
                            ts=rec.ts,
                            text="这一轮没能完成",
                            status="failed",
                            finish_reason="failed",
                        )
                    )
                elif reason == "cancelled" and not cancelled_since_user:
                    items.append(
                        ReplayItem(
                            kind="status",
                            seq=rec.seq,
                            ts=rec.ts,
                            text="这一轮已手动停止",
                            status="cancelled",
                            finish_reason="cancelled",
                        )
                    )
            elif rt == RecordType.FINALIZE:
                payload = rec.payload_as(FinalizePayload)
                # 旧版定稿不进用户视图（只留最新一版）
                if latest_finalize_seq.get(payload.grade_call_id) != rec.seq:
                    continue
                items.append(
                    ReplayItem(
                        kind="finalize",
                        seq=rec.seq,
                        ts=rec.ts,
                        call_id=payload.grade_call_id,
                        text=payload.comment,
                        final_text=payload.final_text,
                        decisions=dict(payload.decisions),
                        overrides=dict(payload.overrides),
                        edited_ids=list(payload.edited_ids),
                        kept_ids=list(payload.kept_ids),
                    )
                )
        except ValidationError:
            continue  # 坏载荷只丢它自己这一条

    return items


def extract_last_input_tokens(records: Iterable[Record]) -> int | None:
    """从流水里取**最后一轮**模型回报的上下文 token（用量环的分子）。

    与 ``build_replay`` 分开：回放序列的语义是"给界面画的内容"，用量是另一个维度的
    元数据，不该塞进回放条目。返回最后一个带 ``context_input_tokens`` 的 turn_end；
    没有则为 None（老会话 / 模型没回 usage）。
    """
    last: int | None = None
    for rec in records:
        if rec.record_type is not RecordType.TURN_END:
            continue
        try:
            turn = rec.payload_as(TurnEndPayload)
        except ValidationError:
            continue
        if turn.context_input_tokens is not None:
            last = turn.context_input_tokens
    return last


def extract_session_usage(records: Iterable[Record]) -> Usage | None:
    """把整个会话逐轮用量加成一个合计（界面切回旧会话时状态条靠它回显）。

    与 :func:`extract_last_input_tokens` 分工不同：那条给"最后一轮上下文多大"（圆环的
    分子），这条给"这个会话一共花了多少、其中多少是重复算的"（状态条）。

    ``cached_tokens`` 单独用一个标志记：只要任何一轮真回了这个字段就算"已知"，合计照实
    给（可能是 0）；一轮都没回就保持 None —— 未知与 0 是两件事。整个会话一条 usage 都
    没有（老会话 / 假模型）时返回 None。
    """
    total_in = 0
    total_out = 0
    total_cached = 0
    saw_usage = False
    saw_cached = False
    for rec in records:
        if rec.record_type is not RecordType.TURN_END:
            continue
        try:
            turn = rec.payload_as(TurnEndPayload)
        except ValidationError:
            continue
        if turn.usage is None:
            continue
        saw_usage = True
        total_in += turn.usage.input_tokens or 0
        total_out += turn.usage.output_tokens or 0
        if turn.usage.cached_tokens is not None:
            saw_cached = True
            total_cached += turn.usage.cached_tokens
    if not saw_usage:
        return None
    return Usage(
        input_tokens=total_in,
        output_tokens=total_out,
        cached_tokens=total_cached if saw_cached else None,
    )
