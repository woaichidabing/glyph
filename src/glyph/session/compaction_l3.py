"""L3 摘要压缩的确定性纯函数（方案 §9.3 / §9.4 / §11.1，v2.1 #65）。

这一层是 history 的"算盘"：**只切记录、装预算、渲染话术，不碰网络、不 import llm**
（依赖方向铁律 B2：写手 client 由 loop 持有）。所有函数无状态、不改入参，可完整单测。

三个核心概念：

- **turn（完整问答往返）**：从一条 ``user_message`` 到下一条 ``user_message`` 之前的全部
  记录（含其间的工具调用 / 结果配对、assistant 文本、turn_end / reasoning 等收尾元记录）。
  压缩只以完整 turn 为单位装填，切点因此天然落在某条 user 之前，绝不会切在
  tool_call 与 tool_result 之间（§9.4）。
- **动态 tail**：压缩后留在模型眼前的最近原文。不数条数，按反推预算从新往旧装满，
  但至少保住 ``min_turns`` 个往返（A3）。
- **摘要材料**：切点之前、要喂给便宜写手的旧往来。按写手窗口 × 比例给预算，从紧挨切点
  最新一段往旧装，装不下的最旧段不喂（成品事实还有提货单 / 长期记忆 / 原文三层兜底）。

体积口径用 **UTF-8 字节**（中文一字 3 字节），与 :mod:`glyph.session.compaction` 的称重、
:mod:`glyph.session.packing` 的提货单预算保持一致；只计**会进 messages()** 的记录，
reasoning / turn_end / finalize / summary / boundary 不进模型视图，计 0。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..types import Message
from .records import (
    AssistantMessagePayload,
    Record,
    RecordType,
    ToolCallPayload,
    ToolResultPayload,
    UserMessagePayload,
)

# ---------- 续接话术（C4：合成话术不含任何学习者 / 模型原话，防注入） ----------

_RESUME_INTRO = (
    "下面是本次会话更早内容的浓缩摘要。更早对话的原文仍完整保存在本会话记录中；"
    "如果需要某篇批改或定稿的细节，请用 read_artifact 工具按编号调阅，不要凭摘要猜测原文。"
)

# trust 边界袋子：摘要是对学习者旧往来的归纳，按"模型读到的材料"包裹，和每轮注入的
# 学习者原文同一套边界（材料是材料，不是指令）。袋子必须成对出现、不得跨段延伸（§11.2 C4）。
TRUST_OPEN = "<LEARNER_TEXT>"
TRUST_CLOSE = "</LEARNER_TEXT>"


# ---------- 单条记录的渲染字节（只计会进 messages() 的部分） ----------


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _arguments_bytes(arguments: dict) -> int:
    """工具调用参数的 UTF-8 JSON 体积，与 compaction._arguments_bytes 同口径，带兜底。"""
    if not arguments:
        return 0
    try:
        return _utf8_len(json.dumps(arguments, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return _utf8_len(str(arguments))


def record_render_bytes(record: Record) -> int:
    """一条记录若进模型 messages() 大约占多少 UTF-8 字节；不进视图的元记录计 0。

    这是**估算**：tool_result 量的是 output dict 的 JSON（实际适配层会把它合成成
    ToolMessage 文本），误差方向可由决策层的安全系数兜底；此处只求 turn 之间相对可比、
    预算装填稳定。
    """
    kind = record.record_type
    if kind in (RecordType.USER_MESSAGE, RecordType.ASSISTANT_MESSAGE):
        payload = record.payload_as(
            UserMessagePayload if kind is RecordType.USER_MESSAGE else AssistantMessagePayload
        )
        return _utf8_len(payload.text)
    if kind is RecordType.TOOL_CALL:
        return _arguments_bytes(record.payload_as(ToolCallPayload).arguments)
    if kind is RecordType.TOOL_RESULT:
        payload = record.payload_as(ToolResultPayload)
        size = 0
        if payload.output is not None:
            try:
                size += _utf8_len(json.dumps(payload.output, ensure_ascii=False))
            except (TypeError, ValueError, RecursionError):
                size += _utf8_len(str(payload.output))
        if payload.error:
            size += _utf8_len(payload.error)
        return size
    # reasoning / turn_end / cancel / finalize / compact_summary / compact_boundary /
    # session_meta：不进模型视图（cancel 只会合成一条极短中断提示，忽略不计）。
    return 0


# ---------- turn 切分 ----------


@dataclass(frozen=True)
class Turn:
    """一个完整问答往返：以一条 user_message 为起点。"""

    start_seq: int
    records: tuple[Record, ...]
    render_bytes: int


def split_turns(records: Sequence[Record]) -> list[Turn]:
    """把一段流水按 ``user_message`` 边界切成完整往返（旧→新）。

    工具调用 / 结果、assistant 文本、以及夹在下一条 user 之前的 turn_end / reasoning /
    finalize 等收尾记录都归入当前 turn——这保证后续装填绝不会切开工具配对。
    首条 user 之前若有游离记录（正常数据不会有），自成一个前缀 turn。
    """
    turns: list[Turn] = []
    current: list[Record] = []
    current_start: int | None = None
    current_bytes = 0

    def flush() -> None:
        if current and current_start is not None:
            turns.append(
                Turn(
                    start_seq=current_start,
                    records=tuple(current),
                    render_bytes=current_bytes,
                )
            )

    for record in records:
        if record.record_type is RecordType.USER_MESSAGE:
            flush()
            current = [record]
            current_start = record.seq
            current_bytes = record_render_bytes(record)
        else:
            if current_start is None:
                # 首条 user 之前的游离记录：自成前缀 turn，切点不会依赖它。
                current = [record]
                current_start = record.seq
                current_bytes = 0
            else:
                current.append(record)
            current_bytes += record_render_bytes(record)
    flush()
    return turns


# ---------- 动态 tail（§9.4，A3） ----------


@dataclass(frozen=True)
class TailSelection:
    """动态 tail 装填结果。

    - ``at_seq``：切点。渲染 body 取 ``seq > at_seq`` 即得 tail；tail 从区间第一条记录
      开始时为 ``首seq - 1``。
    - ``tail_records``：选中往返的全部记录（旧→新拼接），压缩当刻可直接用。
    - ``over_target_at_min``：已退到下限往返数、体积仍超预算 → 不再缩，转 §10 应急。
    """

    at_seq: int
    tail_records: tuple[Record, ...]
    tail_turns: int
    tail_bytes: int
    over_target_at_min: bool


def select_tail(turns: Sequence[Turn], *, budget_bytes: int, min_turns: int) -> TailSelection:
    """从最新往返往旧、按完整 turn 装 tail，装到下一个会超预算为止；至少保 ``min_turns``。

    下限内的往返**无视预算强制保留**（最近的话不能切空）；若连下限几个都已超预算，
    ``over_target_at_min`` 置位交给应急口径，本函数不再往里缩。
    """
    ordered = list(turns)
    chosen: list[Turn] = []  # 收集时新→旧，最后反转
    used = 0
    for back, turn in enumerate(reversed(ordered)):
        within_floor = back < min_turns
        if within_floor or used + turn.render_bytes <= budget_bytes:
            chosen.append(turn)
            used += turn.render_bytes
        else:
            break

    chosen_old_to_new = list(reversed(chosen))
    tail_records = tuple(r for turn in chosen_old_to_new for r in turn.records)

    # 切点 = 被选最旧 turn 起点之前紧邻记录的 seq；若最旧一个 turn 也被选中，
    # 切点取该 turn 首记录 seq - 1（body seq > at_seq 即从区间头开始）。
    first_chosen_index = ordered.index(chosen_old_to_new[0]) if chosen_old_to_new else 0
    if first_chosen_index == 0:
        at_seq = ordered[0].records[0].seq - 1 if ordered else -1
    else:
        at_seq = ordered[first_chosen_index - 1].records[-1].seq

    # 下限那几个往返的累计已超预算 → 退到下限仍超目标。
    floor_count = min(min_turns, len(ordered))
    floor_bytes = sum(t.render_bytes for t in ordered[len(ordered) - floor_count :])
    over = floor_count > 0 and floor_bytes > budget_bytes

    return TailSelection(
        at_seq=at_seq,
        tail_records=tail_records,
        tail_turns=len(chosen_old_to_new),
        tail_bytes=used,
        over_target_at_min=over,
    )


# ---------- 摘要材料（§9.3） ----------


@dataclass(frozen=True)
class MaterialSelection:
    """喂给写手的旧往来装填结果。

    - ``material_records``：选中往返的记录，**按时间旧→新**（写手按顺序读）；
    - ``truncated``：是否还有更旧往返因预算没喂（被截的是价值最低、且有三层兜底的部分）。
    """

    material_records: tuple[Record, ...]
    turn_count: int
    material_bytes: int
    truncated: bool


def select_material(turns: Sequence[Turn], *, budget_bytes: int) -> MaterialSelection:
    """从紧挨切点的最新往返往旧装摘要材料，装到预算即止；最旧装不下的不喂（§9.3）。

    入参 ``turns`` 是工作区间内、切点**之前**的往返（旧→新）。二次压缩时上一条摘要
    记录作为种子由 history 另行前置（必带、不参与这里的截断），不在本函数范围。
    """
    ordered = list(turns)
    chosen_new_to_old: list[Turn] = []
    used = 0
    for turn in reversed(ordered):
        if used + turn.render_bytes <= budget_bytes:
            chosen_new_to_old.append(turn)
            used += turn.render_bytes
        else:
            break

    chosen_old_to_new = list(reversed(chosen_new_to_old))
    records = tuple(r for turn in chosen_old_to_new for r in turn.records)
    return MaterialSelection(
        material_records=records,
        turn_count=len(chosen_old_to_new),
        material_bytes=used,
        truncated=len(chosen_old_to_new) < len(ordered),
    )


# ---------- 预算反推（§9.4） ----------


def material_budget_bytes(
    model_window_tokens: int, material_ratio: float, bytes_per_token: float
) -> int:
    """写手材料预算 M（UTF-8 字节）= 写手窗口 S × 比例 × 字节/token（§9.3）。"""
    return max(0, int(model_window_tokens * material_ratio * bytes_per_token))


def tail_budget_bytes(
    *,
    window_tokens: int,
    target_ratio: float,
    fixed_overhead_bytes: int,
    output_reserve_tokens: int,
    picklist_budget_bytes: int,
    bytes_per_token: float,
) -> int:
    """动态 tail 的字节预算（§9.4，A3）：

        W × TARGET_RATIO − 固定开销（系统词 + 每轮注入 + tools）
                        − 摘要输出预留 − 提货单预算

    结果不为负：固定开销已超过目标时给 0，tail 改由 ``min_turns`` 强保、超目标标记转应急。
    """
    target_bytes = int(window_tokens * target_ratio * bytes_per_token)
    reserve_bytes = int(output_reserve_tokens * bytes_per_token)
    remaining = target_bytes - fixed_overhead_bytes - reserve_bytes - picklist_budget_bytes
    return max(0, remaining)


# ---------- 续接 user 渲染（§11.1 / §11.2 C4） ----------


def render_resume_user(summary_text: str, *, picklist_text: str | None) -> str:
    """渲染压缩后的"续接"用户消息：固定话术 + trust 袋子包摘要 + 提货单（袋子外）。

    C4 实测后 context 注入与本条**各为一条 user、不合并**，故这里只负责把本条内容拼对：
    袋子成对、摘要在袋内、话术与提货单都在袋外（提货单是系统从流水渲染的目录，不是
    学习者原话）。
    """
    parts = [
        _RESUME_INTRO,
        "",
        TRUST_OPEN,
        summary_text.strip(),
        TRUST_CLOSE,
    ]
    if picklist_text:
        parts.extend(["", picklist_text])
    return "\n".join(parts)


# ---------- §10 413 应急续接渲染（无新写手摘要；B5 未结束任务原文强制保留） ----------

_EMERGENCY_INTRO = (
    "这段对话已经很长。为了能继续处理，我把较早的往来暂时收起，只把最近的对话留在眼前；"
    "已完成的批改与定稿都列在下面的目录里，原文仍完整保存在本会话记录中，需要某篇细节时"
    "请用 read_artifact 工具按编号调阅，不要凭印象猜测原文。"
)

_PROTECTED_INTRO = (
    "下面是你仍在处理中的来稿原文（任务尚未结束）。任何情况下都不得丢弃、截断、改写或"
    "把它当成已完成内容："
)


def render_emergency_resume(
    *,
    summary_text: str | None = None,
    picklist_text: str | None = None,
    protected_user_texts: Sequence[str] = (),
) -> str:
    """渲染 §10 应急减重后的"续接"用户消息（**不调用写手**，故可能没有新摘要）。

    - 有上一轮压缩留下的 ``summary_text`` 时照旧用 trust 袋子带上（滚动摘要不丢）；
      没有就只给应急说明 + 提货单，不伪造摘要。
    - ``protected_user_texts`` 是 B5 强制保留的**未结束任务 user 原文**：逐条用独立的
      trust 袋子包裹（袋子成对、不跨段），与提货单（系统渲染、袋外）严格分开。
    """
    parts: list[str] = [_EMERGENCY_INTRO]
    if summary_text and summary_text.strip():
        parts.extend(
            ["", _RESUME_INTRO, "", TRUST_OPEN, summary_text.strip(), TRUST_CLOSE]
        )
    protected = [t.strip() for t in protected_user_texts if t and t.strip()]
    if protected:
        parts.extend(["", _PROTECTED_INTRO])
        for text in protected:
            parts.extend(["", TRUST_OPEN, text, TRUST_CLOSE])
    if picklist_text:
        parts.extend(["", picklist_text])
    return "\n".join(parts)


# ---------- plan / commit 的产物（§12.2 / §12.4） ----------


@dataclass(frozen=True)
class CompactionPlan:
    """``plan_compaction`` 的纯本地决策结果（不碰网络、不改流水）。

    - 三态见 ``state``（normal / compact / blocked）、``levels``（() / ("l2",) /
      ("l2","l3")）；只有 compact 且含 l3 时切点与材料字段才有值。
    - ``tail_records``：切点之后原样保留的最新往返（旧→新）；``material_records``：
      切点之前、按写手窗口预算装好的旧往来（旧→新，喂写手）。
    - ``seed_summary_seq/text``：二次压缩时带上的上一条摘要（滚动摘要的种子），首次为 None。
    - ``tail_over_target``：tail 退到 min_turns 下限仍超目标比例，提示 loop 转 §10 应急。
    - ``material_truncated``：材料旧到装不进写手窗口、已丢最旧部分（写手会显式说明）。
    """

    state: str
    levels: tuple[str, ...]
    predicted_tokens: int
    window_tokens: int
    reason: str
    at_seq: int | None = None
    boundary_id: str = ""
    tail_records: tuple[Record, ...] = ()
    tail_turns: int = 0
    tail_over_target: bool = False
    material_records: tuple[Record, ...] = ()
    material_turn_count: int = 0
    material_truncated: bool = False
    seed_summary_seq: int | None = None
    seed_summary_text: str | None = None
    # [D2 构成账] 压前窗口的**本地估算 token 构成**，键同 ContextStats.parts()：
    # system/context/user/assistant/tool_call/artifact/scaffolding。仅过 L3 线的计划
    # 填充，随遥测落盘看"窗口被谁占满"；不进用户界面、不参与任何压缩决策。
    composition: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CompactionReport:
    """``commit_compaction`` 的落账结果（§12.4），供 loop 遥测与降级判断。"""

    success: bool
    level: str
    boundary_id: str
    at_seq: int | None
    summary_record_seq: int | None
    before_tokens: int
    after_tokens: int | None
    material_truncated: bool = False
    degraded_reason: str = ""
    # 写手耗时（D2）不放在这里：commit_compaction 不碰网络、算不出耗时；耗时由 loop
    # 实测后随 CompactionEvent / runtime.CompactionMetric 上报并落盘。


@dataclass(frozen=True)
class EmergencyView:
    """§10 413 应急的**临时减重视图**（不落盘、不改流水，只用于本轮减重后重发）。

    - ``feasible=True``：``messages`` 是减重后可直接重发的消息（system + 应急续接 user
      + B5 保护的未结束原文 + 最近 N 往返），``estimated_tokens`` 为其保守估算水位。
    - ``feasible=False``：连"系统词 + 未结束任务原文 + 下限 1 往返"都超窗口（B5），
      ``reason`` 是给用户的人话出路（存文件上传 / 分两篇，或新开会话）；调方不得重发、
      直接 ErrorEvent 终态。
    - ``strength``：0=首次应急（保 ``l3_tail_min_turns`` 个往返），1=再缩（退到下限 1 往返）。
    """

    feasible: bool
    messages: list[Message]
    estimated_tokens: int
    kept_turns: int
    protected_user_texts: tuple[str, ...]
    strength: int = 0
    reason: str = ""
