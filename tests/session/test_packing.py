"""L2 成品装箱（方案 §8，v2.1 #63）的单测：任务状态机、分级提货单、装箱折叠。

装箱是**纯渲染**：输入流水 / 消息，输出折叠后的消息，一个字都不落盘删除。
所以这里的断言都落在"模型会看到什么"，而不是内部存储结构。
"""

from __future__ import annotations

import pytest

from glyph.session import new_session
from glyph.session.packing import (
    GradeAnchor,
    GradeTask,
    pack_artifacts,
    render_picklist,
    scan_grade_tasks,
)
from glyph.session.records import (
    FinalizePayload,
    ToolCallPayload,
    ToolResultPayload,
    UserMessagePayload,
    build_record,
)
from glyph.types import (
    AssistantMessage,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
    UserMessage,
)


def _grade(
    seq: int,
    call_id: str,
    *,
    text: str = "He go to school every day.",
    detections: list[dict] | None = None,
    ok: bool = True,
) -> list:
    """一次 grade_text 调用 + 结果，占两个连续 seq（从 seq 起）。"""
    return [
        build_record(
            seq,
            f"t-{seq}",
            ToolCallPayload(id=call_id, name="grade_text", arguments={"text": text}),
        ),
        build_record(
            seq + 1,
            f"t-{seq + 1}",
            ToolResultPayload(
                call_id=call_id,
                name="grade_text",
                ok=ok,
                output={"summary": "一般现在时第三人称单数漏 s", "detections": detections or []},
            ),
        ),
    ]


def _user(seq: int, text: str = "接着聊点别的"):
    return build_record(seq, f"t-{seq}", UserMessagePayload(text=text))


def _recheck(seq: int, detection_id: str, status: str):
    return build_record(
        seq,
        f"t-{seq}",
        ToolResultPayload(
            call_id=f"rc-{seq}",
            name="recheck_detection",
            output={"id": detection_id, "status": status},
        ),
    )


def _finalize(seq: int, grade_call_id: str):
    return build_record(
        seq,
        f"t-{seq}",
        FinalizePayload(grade_call_id=grade_call_id, kept_ids=["d1"], final_text="He goes."),
    )


def _ids(tasks) -> list[str]:
    return [t.call_id for t in tasks]


def test_new_grade_closes_previous_task_even_without_finalize():
    """信号 2：出现新的 grade_text（新原文）= 上一任务自然关闭，哪怕没定稿。"""
    records = [
        *_grade(1, "g1", detections=[{"id": "d1", "original": "go", "corrected": "goes"}]),
        _user(3),
        *_grade(4, "g2", text="She walk home."),
    ]

    tasks = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)
    by_id = {t.call_id: t for t in tasks}

    assert _ids(tasks) == ["g1", "g2"]
    assert by_id["g1"].closed is True
    assert by_id["g1"].close_signal == "new_grade"
    # 最新任务还没有任何关闭信号，也没有静默满 3 轮 → 仍开着，绝不装箱。
    assert by_id["g2"].closed is False
    assert by_id["g2"].close_signal is None


def test_finalize_is_strongest_close_signal():
    """信号 1：流水中有该 grade 的定稿记录 = 确定关闭，并标注定稿。"""
    records = [
        *_grade(1, "g1"),
        _finalize(3, "g1"),
        _user(4),
    ]

    tasks = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)
    task = tasks[0]

    assert task.closed is True
    assert task.close_signal == "finalize"
    assert task.finalized is True


def test_idle_signal_closes_after_three_quiet_turns():
    """信号 3：最后一个任务之后连续 3 个完整往返没人再碰（无 recheck）→ 静默关闭。"""
    records = [
        *_grade(1, "g1"),
        _user(3),
        _user(4),
        _user(5),
    ]

    tasks = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)
    assert tasks[0].closed is True
    assert tasks[0].close_signal == "idle"


def test_recheck_within_idle_window_reopens_task():
    """静默期内又出现指向该任务的 recheck：静默计数清零，任务重新打开（不关闭）。"""
    records = [
        *_grade(1, "g1", detections=[{"id": "d1", "original": "go", "corrected": "goes"}]),
        _user(3),
        _user(4),
        _recheck(5, "d1", "fixed"),
        _user(6),
        _user(7),
    ]

    tasks = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)
    # 最后一次触碰是 seq5 的 recheck，其后只有 2 个往返，未到 3 → 仍开着。
    assert tasks[0].closed is False


def test_recheck_is_attributed_by_time_order_to_the_open_grade():
    """Detection 没有指向 grade 的外键（B4）：recheck 靠时序归到当时开放的那篇。"""
    records = [
        *_grade(1, "g1", detections=[{"id": "a1", "original": "go", "corrected": "goes"}]),
        _user(3),
        _recheck(4, "a1", "confirmed"),
        *_grade(5, "g2", text="Me have pen.", detections=[{"id": "b1"}]),
        _recheck(7, "b1", "wont_fix"),
    ]

    tasks = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)
    by_id = {t.call_id: t for t in tasks}

    g1_status = {a.id: a.recheck_status for a in by_id["g1"].anchors}
    g2_status = {a.id: a.recheck_status for a in by_id["g2"].anchors}
    assert g1_status == {"a1": "confirmed"}
    assert g2_status == {"b1": "wont_fix"}


def test_recent_window_protects_latest_task_from_packing():
    """最近 K 轮内的任务即使关闭也标记在保护窗口（装箱时排除，宁晚装不装早）。"""
    # g1 之后有新 grade（关闭），但 g2 是最新任务、就在最近窗口内。
    records = [
        *_grade(1, "g1"),
        _user(3),
        *_grade(4, "g2"),
    ]

    tasks = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)
    by_id = {t.call_id: t for t in tasks}

    assert by_id["g2"].in_recent_window is True
    # g1 之后隔了 1 个往返就来 g2，g1 的 grade 之后只有 1 个 user → 仍在最近 3 轮窗口。
    assert by_id["g1"].in_recent_window is True


def test_anchor_keeps_non_regenerable_fields():
    """逐条锚点必须带位置契约字段（id/start/end/original/corrected/type/tier），
    这些压不得（§3.6）；可再生的讲解字段不进锚点。"""
    records = [
        *_grade(
            1,
            "g1",
            detections=[
                {
                    "id": "d9",
                    "start": 3,
                    "end": 5,
                    "aligned": True,
                    "original": "go",
                    "corrected": "goes",
                    "type": "hard",
                    "tier": "error",
                    "explanation_zh": "三单",
                }
            ],
        ),
        _finalize(3, "g1"),
        _user(4),
        _user(5),
        _user(6),
    ]

    task = scan_grade_tasks(records, task_idle_turns=3, keep_recent_turns=3)[0]
    anchor = task.anchors[0]
    assert anchor.id == "d9"
    assert (anchor.start, anchor.end) == (3, 5)
    assert anchor.original == "go"
    assert anchor.corrected == "goes"
    assert (anchor.type, anchor.tier) == ("hard", "error")
    assert task.detection_count == 1
    assert task.finalized is True


# ---------- 切片 2：分级提货单渲染 + 字节预算（§8.3 / A4） ----------


def _packable_task(
    call_id: str,
    *,
    title: str = "He go to school.",
    detections: int = 1,
    finalized: bool = False,
    band: str = "三单漏 s",
    anchors: list[GradeAnchor] | None = None,
) -> GradeTask:
    if anchors is None:
        anchors = [
            GradeAnchor(
                id=f"{call_id}-d1",
                start=0,
                end=2,
                original="go",
                corrected="goes",
                type="hard",
                tier="error",
            )
            for _ in range(detections)
        ]
    return GradeTask(
        call_id=call_id,
        grade_seq=1,
        result_seq=2,
        title=title,
        band=band,
        detection_count=detections,
        anchors=anchors,
        finalized=finalized,
        closed=True,
        close_signal="finalize" if finalized else "new_grade",
        in_recent_window=False,
    )


def test_picklist_skips_open_and_recent_window_tasks():
    """只有「已关闭且出了最近 K 轮窗口」的任务才上提货单；当前任务 / 保护窗口内的不装。"""
    tasks = [
        _packable_task("old1"),
        GradeTask(
            call_id="open1",
            grade_seq=3,
            result_seq=4,
            title="正在改的篇",
            band="",
            detection_count=1,
            closed=False,
            close_signal=None,
            in_recent_window=True,
        ),
    ]
    text = render_picklist(tasks, detail_recent=5, budget_bytes=10_000)

    assert "old1" in text
    assert "正在改的篇" not in text


def test_picklist_force_include_lists_open_task_cut_behind_boundary():
    """§9.6 #65：被 L3 boundary 切在 tail 之外的成品，状态机即便仍判"开放 / 在最近窗"，
    force_include 也强制列上——否则被切走的成品就没有 read_artifact 取回锚点。"""
    tasks = [
        GradeTask(
            call_id="open1",
            grade_seq=3,
            result_seq=4,
            title="被切走的篇",
            band="",
            detection_count=1,
            closed=False,
            close_signal=None,
            in_recent_window=True,
        )
    ]

    # 常规 L2：开放 / 窗口内不列（没有可装箱任务 → None）。
    assert render_picklist(tasks, detail_recent=5, budget_bytes=10_000) is None
    # L3 续接强制纳入：列出该篇（编号 + 标题）。
    text = render_picklist(
        tasks, detail_recent=5, budget_bytes=10_000, force_include=frozenset({"open1"})
    )
    assert "open1" in text
    assert "被切走的篇" in text


def test_picklist_lists_recent_five_in_detail_older_as_single_line():
    """最近 5 篇逐条锚点；更早的退成一行（仍保留 call_id / 篇名 / 条数 / 档位）。"""
    tasks = [_packable_task(f"g{i:02d}", title=f"Sentence number {i}") for i in range(1, 8)]

    text = render_picklist(tasks, detail_recent=5, budget_bytes=100_000)

    # 最新 5 篇（g03..g07）逐条：能看到锚点原文 / 建议。
    for cid in ("g03", "g04", "g05", "g06", "g07"):
        assert f"grade@{cid}" in text
    assert "go" in text and "goes" in text
    # 最旧 2 篇（g01/g02）一行：保留代号与篇名，但不铺开它们的锚点。
    assert "grade@g01" in text and "grade@g02" in text
    assert "Sentence number 1" in text


def test_picklist_single_line_always_keeps_call_id():
    """一行锚点的底线：call_id 必须在，否则模型无法按编号取回（提货单变废纸）。"""
    tasks = [_packable_task("abc-123", title="XYZ")]
    text = render_picklist([tasks[0]], detail_recent=0, budget_bytes=10_000)

    assert "grade@abc-123" in text


def test_picklist_marks_finalized_and_points_to_final_artifact():
    """定稿篇要标"已定稿"，并提示定稿版用 which=final 取回（§8.6）。"""
    tasks = [_packable_task("g1", finalized=True)]
    text = render_picklist(tasks, detail_recent=5, budget_bytes=10_000)

    assert "定稿" in text
    assert "final" in text


def test_picklist_does_not_render_regenerable_explanation():
    """锚点只带不可再生字段；讲解文本（explanation_zh）压根不进提货单。"""
    anchor = GradeAnchor(
        id="d1",
        start=0,
        end=2,
        original="go",
        corrected="goes",
        type="hard",
        tier="error",
        recheck_status=None,
    )
    tasks = [_packable_task("g1", anchors=[anchor])]
    text = render_picklist(tasks, detail_recent=5, budget_bytes=10_000)

    # 提货单里不允许出现可再生讲解的字段名 / 长讲解串（这些只留流水全文）。
    assert "explanation" not in text.lower()
    assert "详细讲解" not in text


def test_picklist_respects_budget_by_degrading_then_omitting_oldest():
    """A4：提货单自身字节预算 ≤ 限额。超预算先把最旧的逐条退成一行，再超就省略最旧一行，
    但必须保留"另有 N 篇已省略 + 先查目录"的提示，且最新内容优先保住。"""
    # 6 篇都够格逐条，但给一个只容得下抬头 + 省略提示 + 最新一篇一行的小预算。
    tasks = [_packable_task(f"g{i:02d}", title=f"Sentence number {i}") for i in range(1, 7)]
    budget = 460

    text = render_picklist(tasks, detail_recent=5, budget_bytes=budget)

    assert text is not None
    assert len(text.encode("utf-8")) <= budget
    # 最新的 g06 必须在（保新），最旧的被省略并给出数量提示。
    assert "grade@g06" in text
    assert "g01" not in text
    assert "篇" in text and ("目录" in text or "read_artifact" in text)


def test_picklist_none_when_no_packable_task():
    assert render_picklist([], detail_recent=5, budget_bytes=10_000) is None


# ---------- 切片 3：装箱折叠（§8.2 / §11，纯渲染、保配对、可逆） ----------


def _grade_tool(cid: str, result_text: str = '{"summary":"x","detections":[]}') -> ToolMessage:
    """一条 grade_text 的 tool 结果消息（与 assistant tool_call 配对，折叠不能删它）。"""
    return ToolMessage(tool_call_id=cid, name="grade_text", content=result_text)


def test_pack_folds_closed_old_grade_result_but_keeps_current_task():
    """已关闭且出窗口的旧成品：tool 结果折成占位 + 插提货单；当前 / 最近窗口的成品原样保留。"""
    long_result = '{"summary":"旧篇","detections":[{"explanation_zh":"一大段可再生讲解"}]}'
    messages = [
        UserMessage(content="改第一篇"),
        _grade_tool("g1", long_result),
        UserMessage(content="改第二篇"),
        _grade_tool("g2", '{"summary":"新篇","detections":[]}'),
    ]
    tasks = [
        _packable_task("g1", title="第一篇"),
        GradeTask(
            call_id="g2",
            grade_seq=4,
            result_seq=5,
            title="第二篇",
            band="",
            detection_count=0,
            closed=False,
            close_signal=None,
            in_recent_window=True,
        ),
    ]

    result = pack_artifacts(messages, tasks, detail_recent=5, budget_bytes=100_000)

    assert result.changed is True
    assert result.packed_call_ids == ["g1"]
    contents = [m.content for m in result.messages]
    # 旧成品的长结果 / 可再生讲解不再出现在上下文……
    assert not any("一大段可再生讲解" in c for c in contents)
    # ……但 tool 配对仍在（tool_call_id=g1 的 tool 消息没被删，只换成占位）。
    g1_tool = [m for m in result.messages if isinstance(m, ToolMessage) and m.tool_call_id == "g1"]
    assert len(g1_tool) == 1
    assert "grade@g1" in g1_tool[0].content
    # 当前任务 g2 的结果原样保留。
    g2_tool = [m for m in result.messages if isinstance(m, ToolMessage) and m.tool_call_id == "g2"]
    assert g2_tool[0].content == '{"summary":"新篇","detections":[]}'
    # 提货单作为 SystemMessage 插入，且含旧篇代号。
    assert any(isinstance(m, SystemMessage) and "grade@g1" in m.content for m in result.messages)


def test_pack_no_change_when_nothing_packable():
    """没有可装箱任务时原样返回（长度、内容都不变），不凭空插提货单。"""
    messages = [UserMessage(content="hi"), _grade_tool("g1")]
    tasks = [
        GradeTask(
            call_id="g1",
            grade_seq=2,
            result_seq=3,
            title="t",
            band="",
            detection_count=0,
            closed=False,
            in_recent_window=True,
        )
    ]

    result = pack_artifacts(messages, tasks, detail_recent=5, budget_bytes=10_000)

    assert result.changed is False
    assert result.packed_call_ids == []
    assert result.picklist is None
    assert result.messages == messages


def test_pack_does_not_mutate_input_messages():
    """装箱是纯渲染：入参消息列表与消息对象一条都不许被改动（可逆、可重放）。"""
    messages = [UserMessage(content="改第一篇"), _grade_tool("g1", '{"detections":[]}')]
    snapshot = list(messages)
    tasks = [_packable_task("g1")]

    pack_artifacts(messages, tasks, detail_recent=5, budget_bytes=10_000)

    assert messages == snapshot
    assert all(isinstance(m, type(snapshot[i])) for i, m in enumerate(messages))


# ---------- 切片 4：history 接线（粘滞开关 + L1 后装箱 + 缓存键） ----------


async def _record_grade(history, cid: str, text: str, output: dict) -> None:
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id=cid, name="grade_text", arguments={"text": text})]
        )
    )
    await history.record_tool_result(
        cid, ToolResult(tool_name="grade_text", ok=True, output=output)
    )


def _tool(msgs, cid: str) -> ToolMessage:
    return next(m for m in msgs if isinstance(m, ToolMessage) and m.tool_call_id == cid)


_OLD_OUTPUT = {
    "summary": "旧",
    "detections": [{"id": "d1", "explanation_zh": "旧篇一大段可再生讲解XYZ"}],
}


@pytest.mark.asyncio
async def test_history_does_not_pack_before_l2_is_armed(tmp_path):
    """粘滞开关置位前，L2 绝不生效——哪怕旧任务早已关闭（触发权在 plan_compaction，#66）。"""
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await history.record_user_message("改第一篇")
    await _record_grade(history, "g1", "He go.", _OLD_OUTPUT)
    await history.record_finalize(
        grade_call_id="g1", decisions={}, final_text="He goes.", kept_ids=["d1"]
    )
    for i in range(4):
        await history.record_user_message(f"别的话题{i}")

    msgs = history.messages()

    assert "旧篇一大段可再生讲解XYZ" in _tool(msgs, "g1").content
    assert not any(isinstance(m, SystemMessage) and "已归档" in m.content for m in msgs)
    history.close()


@pytest.mark.asyncio
async def test_history_packs_stickily_after_armed_and_keeps_current_task(tmp_path):
    """置位后：旧定稿篇折成提货单占位、当前任务原样；之后每轮都保持装箱（粘滞、不回弹），
    且置位会让 messages 缓存按新键重建。"""
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await history.record_user_message("改第一篇")
    await _record_grade(history, "g1", "He go.", _OLD_OUTPUT)
    await history.record_finalize(
        grade_call_id="g1", decisions={}, final_text="He goes.", kept_ids=["d1"]
    )
    for i in range(3):
        await history.record_user_message(f"别的话题{i}")
    await history.record_user_message("改第二篇")
    await _record_grade(
        history,
        "g2",
        "She walk.",
        {"summary": "新篇", "detections": []},
    )

    before = history.messages()
    assert "旧篇一大段可再生讲解XYZ" in _tool(before, "g1").content

    history.mark_l2_packed()  # #66 的 plan_compaction 将来在 L2 线翻转它

    first = history.messages()
    # 旧篇：长结果消失、换成占位，提货单 SystemMessage 已插入。
    assert "旧篇一大段可再生讲解XYZ" not in _tool(first, "g1").content
    assert "grade@g1" in _tool(first, "g1").content
    assert any(isinstance(m, SystemMessage) and "grade@g1" in m.content for m in first)
    # 当前任务 g2 一字不动。
    assert '"新篇"' in _tool(first, "g2").content
    # 粘滞：再取一次仍是装箱后的形态，不回弹。
    assert history.messages() == first
    history.close()
