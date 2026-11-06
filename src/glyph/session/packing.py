"""L2 · 成品装箱（方案 §8，v2.1 #63）——纯规则、免费、可恢复减载。

思路不是"删掉旧成品"，而是把已结束任务的批改 / OCR / 分析结果从"摆在台面"改成
"放上货架、给一张提货单"：

- 上下文里，旧成品的 ToolMessage 折叠成一条**锚点（提货单）**；
- 成品全文一字不删，继续躺在本地会话流水（只追加 JSONL）里；
- 模型需要细节时，自己调只读工具按编号取回（``read_artifact``，#64 交付）。

本模块**全是纯函数 / 只读扫描**：输入流水记录或已渲染消息，输出折叠结果，不落盘、
不碰网络、不改入参，可完整单测。任务关闭判定（§8.2 状态机）刻意做成确定性的，
绝不允许"看起来聊完了就装箱"——宁可晚装，不可装早。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import DEFAULT_FOLD_KEEP_RECENT, DEFAULT_TASK_IDLE_TURNS
from ..types import Message, SystemMessage, ToolMessage
from .records import (
    FinalizePayload,
    Record,
    RecordType,
    ToolCallPayload,
    ToolResultPayload,
)

# 关闭信号（确定性从强到弱，§8.2 B4）
SIGNAL_FINALIZE = "finalize"  # 信号 1：用户已拍板定稿（#69）
SIGNAL_NEW_GRADE = "new_grade"  # 信号 2：出现新的批改，上一任务自然关闭
SIGNAL_IDLE = "idle"  # 信号 3：连续若干往返无人再碰，静默关闭

_TITLE_MAX = 24
_BAND_MAX = 40


@dataclass(frozen=True)
class GradeAnchor:
    """一条 detection 的**不可再生**锚点（位置契约，§3.6：这些字段压不得）。

    可再生的讲解字段（explanation_zh / reason_zh）不进上下文、只留流水全文，
    需要时由 read_artifact 取回或重新生成。
    """

    id: str
    start: int | None
    end: int | None
    original: str
    corrected: str
    type: str
    tier: str
    recheck_status: str | None = None


@dataclass(frozen=True)
class GradeTask:
    """流水中识别出的一个批改任务（相邻两次 grade_text 之间归同一篇原文的全部记录）。"""

    call_id: str
    grade_seq: int
    result_seq: int | None
    title: str
    band: str
    detection_count: int
    anchors: list[GradeAnchor] = field(default_factory=list)
    finalized: bool = False
    closed: bool = False
    close_signal: str | None = None
    # 在最近 K 轮保护窗口内：即使已关闭也不装箱（当前可能正在逐条裁决 / 复核）。
    in_recent_window: bool = True


def _first_line(text: str) -> str:
    """篇名 / 首句片段：取第一个句读前、再限长，给提货单当人类可读标题。"""
    head = text.strip().replace("\n", " ")
    for punct in ("。", "！", "？", ".", "!", "?", "；", ";"):
        idx = head.find(punct)
        if 0 <= idx < _TITLE_MAX:
            head = head[: idx + 1]
            break
    if len(head) > _TITLE_MAX:
        head = head[:_TITLE_MAX].rstrip() + "…"
    return head or "（无文本）"


def _band_from(output: dict, detection_count: int) -> str:
    """一句话档位 / 整体结论：优先用模型 summary，没有就报问题条数。"""
    summary = str(output.get("summary") or "").strip()
    if summary:
        return summary if len(summary) <= _BAND_MAX else summary[:_BAND_MAX].rstrip() + "…"
    return f"{detection_count} 处问题" if detection_count else "无问题"


def scan_grade_tasks(
    records: list[Record],
    *,
    task_idle_turns: int = DEFAULT_TASK_IDLE_TURNS,
    keep_recent_turns: int = DEFAULT_FOLD_KEEP_RECENT,
) -> list[GradeTask]:
    """扫描全量流水，识别每个 grade_text 任务并判定关闭状态（§8.2 状态机）。**纯函数**。

    数据约束（B4）：``Detection`` 没有指向 grade call 的外键，复核归属只能靠流水时序——
    一次 recheck 归属于"发生时还开放的那一篇"（它之前最近的一次 grade_text）。
    """
    # 第一遍：按 call_id 收集 grade 调用 / 结果、定稿、复核、user 往返位置。
    grade_calls: dict[str, dict] = {}
    grade_order: list[str] = []
    finalize_seqs: dict[str, int] = {}
    rechecks: list[tuple[int, str, str | None]] = []
    user_seqs: list[int] = []

    for record in records:
        rtype = record.record_type
        if rtype == RecordType.TOOL_CALL:
            call = record.payload_as(ToolCallPayload)
            if call.name == "grade_text" and call.id not in grade_calls:
                grade_calls[call.id] = {
                    "call_id": call.id,
                    "call_seq": record.seq,
                    "result_seq": None,
                    "text": str(call.arguments.get("text") or ""),
                    "output": {},
                }
                grade_order.append(call.id)
        elif rtype == RecordType.TOOL_RESULT:
            result = record.payload_as(ToolResultPayload)
            if result.name == "grade_text" and result.call_id in grade_calls:
                slot = grade_calls[result.call_id]
                if slot["result_seq"] is None:
                    slot["result_seq"] = record.seq
                    slot["output"] = result.output or {}
            elif result.name == "recheck_detection":
                out = result.output or {}
                rechecks.append((record.seq, str(out.get("id") or ""), out.get("status")))
        elif rtype == RecordType.USER_MESSAGE:
            user_seqs.append(record.seq)
        elif rtype == RecordType.FINALIZE:
            payload = record.payload_as(FinalizePayload)
            finalize_seqs[payload.grade_call_id] = record.seq

    def open_grade_at(seq: int) -> str | None:
        """seq 时刻开放的 grade：call_seq 严格早于 seq 的最近一篇。"""
        current = None
        for cid in grade_order:
            if grade_calls[cid]["call_seq"] < seq:
                current = cid
            else:
                break
        return current

    # 复核结论按归属 grade 汇总到 detection id（同一 id 多次复核，后写覆盖为最新）。
    recheck_status: dict[str, dict[str, str]] = {}
    last_touch: dict[str, int] = {}
    for cid in grade_order:
        last_touch[cid] = grade_calls[cid]["result_seq"] or grade_calls[cid]["call_seq"]
    for seq, det_id, status in rechecks:
        owner = open_grade_at(seq)
        if owner is None or not det_id or not status:
            continue
        recheck_status.setdefault(owner, {})[det_id] = status
        last_touch[owner] = max(last_touch[owner], seq)

    tasks: list[GradeTask] = []
    total = len(grade_order)
    for index, cid in enumerate(grade_order):
        slot = grade_calls[cid]
        output: dict = slot["output"]
        raw_dets = output.get("detections") if isinstance(output, dict) else None
        raw_dets = raw_dets if isinstance(raw_dets, list) else []
        owned_recheck = recheck_status.get(cid, {})

        anchors: list[GradeAnchor] = []
        for det in raw_dets:
            if not isinstance(det, dict):
                continue
            det_id = str(det.get("id") or "")
            embedded = det.get("recheck") if isinstance(det.get("recheck"), dict) else {}
            anchors.append(
                GradeAnchor(
                    id=det_id,
                    start=det.get("start"),
                    end=det.get("end"),
                    original=str(det.get("original") or ""),
                    corrected=str(det.get("corrected") or ""),
                    type=str(det.get("type") or ""),
                    tier=str(det.get("tier") or ""),
                    # 归属复核结论优先；否则用成品里已挂的 recheck.status。
                    recheck_status=owned_recheck.get(det_id) or embedded.get("status"),
                )
            )

        users_after = sum(1 for useq in user_seqs if useq > last_touch[cid])
        finalized = cid in finalize_seqs

        if finalized:
            closed, signal = True, SIGNAL_FINALIZE
        elif index < total - 1:
            closed, signal = True, SIGNAL_NEW_GRADE
        elif users_after >= task_idle_turns:
            closed, signal = True, SIGNAL_IDLE
        else:
            closed, signal = False, None

        tasks.append(
            GradeTask(
                call_id=cid,
                grade_seq=slot["call_seq"],
                result_seq=slot["result_seq"],
                title=_first_line(slot["text"]),
                band=_band_from(output, len(raw_dets)),
                detection_count=len(raw_dets),
                anchors=anchors,
                finalized=finalized,
                closed=closed,
                close_signal=signal,
                in_recent_window=users_after < keep_recent_turns,
            )
        )

    return tasks


# ---------- 分级提货单渲染（§8.3）+ 字节预算（A4） ----------

_PICKLIST_HEADER = (
    "【已归档的历史批改 · 按需取回】\n"
    "以下是此前已结束批改的索引。需要某篇原文、完整批改结果或定稿版时，"
    '调 read_artifact(call_id=…, item_id=…, which="result"|"final") 按编号取回，'
    "不要凭记忆复述细节。"
)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _finalized_tag(task: GradeTask, *, detailed: bool) -> str:
    if not task.finalized:
        return ""
    return "｜✅已定稿(可取定稿版 which=final)" if detailed else "｜已定稿"


def _task_line(task: GradeTask) -> str:
    """更早任务的一行锚点：代号 + 篇名 + 条数 + 档位 + 定稿状态（call_id 绝不省）。"""
    return (
        f"· grade@{task.call_id}《{task.title}》{task.detection_count}处"
        f"｜{task.band}{_finalized_tag(task, detailed=False)}"
    )


def _task_detail(task: GradeTask) -> str:
    """最近任务的逐条锚点：铺开每条 detection 的不可再生位置契约 + 取回入口。"""
    head = (
        f"▪ grade@{task.call_id}《{task.title}》{task.detection_count}处"
        f"｜{task.band}{_finalized_tag(task, detailed=True)}"
    )
    lines = [head]
    for anchor in task.anchors:
        status = f"，复核:{anchor.recheck_status}" if anchor.recheck_status else ""
        lines.append(
            f"    · [{anchor.id or '-'}] 「{anchor.original}」→「{anchor.corrected}」"
            f"（{anchor.tier}/{anchor.type}{status}）"
        )
    if not task.anchors:
        lines.append("    · （无逐条问题，取回整份结果即可）")
    return "\n".join(lines)


def _omit_line(omitted: int) -> str:
    return (
        f"…另有 {omitted} 篇更早的成品已省略；需要时先调 read_artifact() "
        "查看成品目录，再按编号取回。"
    )


def render_picklist(
    tasks: list[GradeTask],
    *,
    detail_recent: int,
    budget_bytes: int,
    force_include: frozenset[str] | None = None,
) -> str | None:
    """把**可装箱**任务（已关闭且出了最近 K 轮窗口）渲染成分级提货单。**纯函数**。

    - 最近 ``detail_recent`` 篇给逐条锚点，更早的退成一行；
    - 总 UTF-8 字节不超过 ``budget_bytes``（A4：提货单自己不能堆成新地板）；
    - 超预算时从最旧的逐条退成一行、再省略最旧的一行，但始终保留省略数量与
      "先查目录"提示，并优先保住最新内容；
    - 没有可装箱任务、或预算小到连抬头 + 省略提示都放不下时返回 None（本次不装）。

    ``force_include``（v2.1 #65，§9.6）：L3 续接提货单专用——这些 call_id 对应的成品
    **已被 boundary 切在 tail 之外**，哪怕状态机仍判它"开放 / 在最近 K 窗"（压缩后只隔
    一两个新 user），也必须列上，否则被切走的成品就丢了取回锚点。L2 装箱不传此参。
    """
    forced = force_include or frozenset()
    packable = [
        t for t in tasks if (t.closed and not t.in_recent_window) or t.call_id in forced
    ]
    if not packable:
        return None

    detail_ids = {t.call_id for t in packable[-detail_recent:]} if detail_recent > 0 else set()

    def block(task: GradeTask, form: str) -> str:
        return _task_detail(task) if form == "detail" else _task_line(task)

    # 贪心从新 → 旧铺：最新的最重要，先保新。
    used = _utf8_len(_PICKLIST_HEADER)
    kept: list[tuple[GradeTask, str]] = []  # 顺序为新 → 旧
    omitted = 0
    for task in reversed(packable):
        form = "detail" if task.call_id in detail_ids else "line"
        size = _utf8_len(block(task, form)) + 1  # +1 给换行
        if used + size <= budget_bytes:
            kept.append((task, form))
            used += size
            continue
        if form == "detail":
            size = _utf8_len(_task_line(task)) + 1
            if used + size <= budget_bytes:
                kept.append((task, "line"))
                used += size
                continue
        # 这篇连一行都放不下：它与所有更旧的篇一律省略。
        omitted = packable.index(task) + 1
        break

    # 若发生省略，确保省略提示也放得下；放不下就先把已留里最旧的逐条降级成一行，
    # 仍不够再把最旧的篇并入省略（始终优先保住最新内容）。
    if omitted:
        note = _omit_line(omitted)
        while used + _utf8_len(note) + 1 > budget_bytes and kept:
            task, form = kept[-1]  # kept 末尾即最旧
            if form == "detail":
                line_size = _utf8_len(_task_line(task)) + 1
                detail_size = _utf8_len(_task_detail(task)) + 1
                if used - detail_size + line_size + _utf8_len(note) + 1 <= budget_bytes:
                    kept[-1] = (task, "line")
                    used = used - detail_size + line_size
                    note = _omit_line(omitted)
                    continue
            task, form = kept.pop()
            used -= _utf8_len(block(task, form)) + 1
            omitted += 1
            note = _omit_line(omitted)
        if used + _utf8_len(note) + 1 > budget_bytes:
            return None
        body = [note]
    else:
        body = []

    # 展示顺序改回旧 → 新（省略提示已在最前，代表更早的篇）。
    for task, form in reversed(kept):
        body.append(block(task, form))

    return _PICKLIST_HEADER + "\n" + "\n".join(body)


# ---------- 装箱折叠（§8.2 / §11，纯渲染、保配对、可逆） ----------


@dataclass(frozen=True)
class PackResult:
    """装箱结果。``messages`` 是一份新列表（入参不被改动），可直接替换发给模型。"""

    messages: list[Message]
    packed_call_ids: list[str]
    picklist: str | None
    changed: bool


def _packed_placeholder(call_id: str) -> str:
    return (
        f"[已归档] 本篇批改已收入提货单 grade@{call_id}；需要原文 / 完整结果 / 定稿版时，"
        "调 read_artifact 按编号取回，勿凭记忆复述。"
    )


def pack_artifacts(
    messages: list[Message],
    tasks: list[GradeTask],
    *,
    detail_recent: int,
    budget_bytes: int,
    inject_picklist: bool = True,
) -> PackResult:
    """把已关闭且出了最近 K 轮窗口的 grade_text 成品**就地折叠**成占位，并在归档区开头
    插入一条提货单 ``SystemMessage``。**纯函数、可逆**：

    - 只把成品 ``ToolMessage`` 的 content 替换为一行占位（``model_copy`` 新建，不改原对象），
      **不删消息、不改 tool_call_id**——assistant 的 tool_call 与 tool 结果始终成对，
      模型侧 / API 侧结构合法；
    - 当前任务与最近 K 轮窗口内的成品一字不动；
    - 入参列表与其余消息不会被修改（未命中消息原样透传）。
    """
    packable = {t.call_id for t in tasks if t.closed and not t.in_recent_window}
    picklist = render_picklist(tasks, detail_recent=detail_recent, budget_bytes=budget_bytes)

    packed: list[str] = []
    out: list[Message] = []
    first_fold_index: int | None = None

    for message in messages:
        if isinstance(message, ToolMessage) and message.name == "grade_text":
            cid = message.tool_call_id
            if cid in packable:
                if cid not in packed:
                    packed.append(cid)
                if first_fold_index is None:
                    first_fold_index = len(out)
                out.append(message.model_copy(update={"content": _packed_placeholder(cid)}))
                continue
        out.append(message)

    if not packed:
        return PackResult(messages=list(messages), packed_call_ids=[], picklist=None, changed=False)

    # 折叠已发生；提货单仅在调用方要求时注入（L3 续接 user 已自带一份全量提货单，不重复）。
    if inject_picklist and picklist is not None:
        out.insert(
            first_fold_index if first_fold_index is not None else 0,
            SystemMessage(content=picklist),
        )
        return PackResult(messages=out, packed_call_ids=packed, picklist=picklist, changed=True)

    return PackResult(messages=out, packed_call_ids=packed, picklist=None, changed=True)


# ---------- §8.4 B3：read_artifact 取回结果的去重 / K 轮折回（#64）----------
def _artifact_target_key(arguments: dict) -> tuple[str, str, str]:
    """一次取回的"目标成品键"：(成品代号, 条目 id, result/final)。

    刻意用 read_artifact **入参里指向的成品** call_id，而不是 read_artifact 自己那次
    调用的 tool_call_id——后者每次调用都换新，拿它判"同一篇"会把每次取回都当成新目标。
    """
    return (
        str(arguments.get("call_id") or "(catalog)"),
        str(arguments.get("item_id") or ""),
        str(arguments.get("which") or "result"),
    )


def _folded_artifact_line(key: tuple[str, str, str], users_after: int) -> str:
    when = "本轮早些时候" if users_after == 0 else f"约 {users_after} 轮前"
    cid, item_id, which = key
    if cid == "(catalog)":
        return (
            f"[已折叠] {when}查过一次成品目录，完整清单以最近一次目录为准；"
            "需要时可再次调 read_artifact（不传 call_id）。"
        )
    target = f"grade@{cid}"
    if item_id:
        target += f" 条目 {item_id}"
    if which == "final":
        target += "（定稿版）"
    return (
        f"[已折叠] {when}已调阅 {target}，内容以该目标最近一次取回为准、此处不重复；"
        "如仍需要，可再次调 read_artifact 取回。"
    )


def fold_artifact_reads(
    messages: list[Message],
    records: list[Record],
    *,
    keep_recent_turns: int = DEFAULT_FOLD_KEEP_RECENT,
) -> tuple[list[Message], bool]:
    """折叠 read_artifact 的取回结果（§8.4 B3），返回 ``(新消息列表, 是否改动)``。

    1. **同目标去重**：同键（call_id + item_id + which）多次取回，只留**最近一次**完整，
       早先的折成一行；
    2. **K 轮折回**：最近一次取回若已过 ``keep_recent_turns`` 个用户往返未再引用，也折回
       一行（成品本身仍在提货单上、可再取——取回结果不享永久席位）；
    3. 最近 K 轮内的取回完整保留；**失败的取回（友好报错）很短，不折叠**，留给模型换编号。

    轮次口径与装箱一致：按流水里该次取回（TOOL_CALL）之后的 USER_MESSAGE 条数计，
    不依赖消息对象上的位置。入参不被修改。
    """
    # read_artifact 自己的调用 id -> (seq, 目标键)；以及各用户往返的 seq
    call_info: dict[str, tuple[int, tuple[str, str, str]]] = {}
    user_seqs: list[int] = []
    for record in records:
        rtype = record.record_type
        if rtype == RecordType.TOOL_CALL:
            call = record.payload_as(ToolCallPayload)
            if call.name == "read_artifact":
                call_info[call.id] = (record.seq, _artifact_target_key(call.arguments))
        elif rtype == RecordType.USER_MESSAGE:
            user_seqs.append(record.seq)

    if not call_info:
        return list(messages), False

    # 每个目标键最近一次取回的调用 id（seq 最大）
    latest_for_key: dict[tuple[str, str, str], str] = {}
    for call_id, (seq, key) in call_info.items():
        incumbent = latest_for_key.get(key)
        if incumbent is None or seq > call_info[incumbent][0]:
            latest_for_key[key] = call_id

    out: list[Message] = []
    changed = False
    for message in messages:
        if (
            isinstance(message, ToolMessage)
            and message.name == "read_artifact"
            and not message.is_error
            and message.tool_call_id in call_info
        ):
            seq, key = call_info[message.tool_call_id]
            users_after = sum(1 for useq in user_seqs if useq > seq)
            is_latest = latest_for_key.get(key) == message.tool_call_id
            if not (is_latest and users_after < keep_recent_turns):
                out.append(
                    message.model_copy(
                        update={"content": _folded_artifact_line(key, users_after)}
                    )
                )
                changed = True
                continue
        out.append(message)
    return out, changed
