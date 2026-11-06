"""read_artifact 的会话侧只读成品读取器（方案 §8.4 / §8.6，v2.1 #64）单测。

读取器从**当前会话全量流水**检索（含将来 L3 boundary 之前的记录）：
- 不传 call_id → 全量一行目录；
- which=result → 原始批改成品（附原文）；item_id → 单条 detection；
- which=final → 用户最新定稿版。
全程只读、不跨会话、不碰磁盘路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from glyph.session import new_session
from glyph.session.packing import fold_artifact_reads
from glyph.types import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    ToolResult,
)


async def _grade(history, cid: str, text: str, output: dict) -> None:
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id=cid, name="grade_text", arguments={"text": text})]
        )
    )
    await history.record_tool_result(
        cid, ToolResult(tool_name="grade_text", ok=True, output=output)
    )


@pytest.fixture
async def seeded_history(tmp_path) -> AsyncIterator:
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await history.record_user_message("改第一篇")
    await _grade(
        history,
        "g1",
        "He go to school.",
        {
            "summary": "三单漏 s",
            "detections": [
                {"id": "d1", "original": "go", "corrected": "goes", "tier": "error"},
                {"id": "d2", "original": "to", "corrected": "", "tier": "style"},
            ],
        },
    )
    await history.record_finalize(
        grade_call_id="g1",
        decisions={"d1": "accepted"},
        final_text="He goes to school.",
        kept_ids=["d1"],
    )
    yield history
    history.close()


@pytest.mark.asyncio
async def test_catalog_lists_every_grade_with_finalized_flag(seeded_history):
    """目录列全量成品（不只装箱的），带代号 / 篇名 / 条数 / 定稿标记。"""
    catalog = seeded_history.artifact_catalog()

    assert catalog["count"] == 1
    item = catalog["items"][0]
    assert item["call_id"] == "g1"
    assert item["detection_count"] == 2
    assert item["finalized"] is True
    assert "He go to school" in item["title"]


@pytest.mark.asyncio
async def test_read_result_returns_full_output_plus_source_text(seeded_history):
    """which=result（默认）返回完整批改成品，并附上原文（模型取回旧稿要能看到原话）。"""
    result = seeded_history.read_artifact("g1")

    assert result is not None
    assert result["call_id"] == "g1"
    assert result["source_text"] == "He go to school."
    assert result["summary"] == "三单漏 s"
    assert [d["id"] for d in result["detections"]] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_read_single_item_returns_just_that_detection(seeded_history):
    item = seeded_history.read_artifact("g1", item_id="d2")

    assert item is not None
    assert item["call_id"] == "g1"
    assert item["item"]["id"] == "d2"
    assert item["item"]["tier"] == "style"


@pytest.mark.asyncio
async def test_read_missing_item_returns_none(seeded_history):
    assert seeded_history.read_artifact("g1", item_id="nope") is None


@pytest.mark.asyncio
async def test_read_final_returns_latest_finalized_text(seeded_history):
    """which=final 读定稿版；同一篇反复定稿只追加，读取一律取最新（§8.6）。"""
    await seeded_history.record_finalize(
        grade_call_id="g1",
        decisions={},
        final_text="He goes to school every day.",
        kept_ids=["d9"],
    )

    result = seeded_history.read_artifact("g1", which="final")

    assert result is not None
    assert result["call_id"] == "g1"
    # 字段也来自最新一条定稿，而不是第一次的 kept_ids
    assert result["final_text"] == "He goes to school every day."
    assert result["kept_ids"] == ["d9"]


@pytest.mark.asyncio
async def test_read_final_without_finalize_returns_none(tmp_path):
    history = await new_session(system_prompt="s", model="m", cwd=str(tmp_path), root=tmp_path)
    await _grade(history, "g0", "Me have pen.", {"summary": "", "detections": []})

    assert history.read_artifact("g0", which="final") is None
    history.close()


@pytest.mark.asyncio
async def test_read_unknown_call_id_returns_none(seeded_history):
    assert seeded_history.read_artifact("does-not-exist") is None


# ---------- §8.4 B3：取回结果去重 / K 轮折回 ----------
async def _read(history, tool_call_id: str, arguments: dict, marker: str) -> None:
    """录一次 read_artifact 工具调用 + 成功取回（正文用 marker 便于辨认是否被折叠）。"""
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id=tool_call_id, name="read_artifact", arguments=arguments)]
        )
    )
    await history.record_tool_result(
        tool_call_id,
        ToolResult(tool_name="read_artifact", ok=True, output={"full": marker}),
    )


async def _turn(history, question: str, answer: str) -> None:
    await history.record_user_message(question)
    await history.record_assistant_message(AssistantMessage(blocks=[TextBlock(text=answer)]))


def _fold(history, keep=3):
    records = history._records_after_last_boundary(history._records)
    messages = history._build_messages(include_system=True, transient=None)
    return fold_artifact_reads(messages, records, keep_recent_turns=keep)


def _read_messages(out) -> list:
    return [m for m in out if isinstance(m, ToolMessage) and m.name == "read_artifact"]


@pytest.mark.asyncio
async def test_repeated_fetch_keeps_only_latest_full(tmp_path):
    """钉测试①：同一篇连取 3 次，上下文只有 1 份完整结果 + 2 行折叠，配对 id 不丢。"""
    history = await new_session(system_prompt="s", model="m", cwd=str(tmp_path), root=tmp_path)
    args = {"call_id": "g1", "which": "result"}
    await _read(history, "r1", args, "FULL_1")
    await _read(history, "r2", args, "FULL_2")
    await _read(history, "r3", args, "FULL_3")

    out, changed = _fold(history)
    assert changed is True

    reads = _read_messages(out)
    full = [m for m in reads if "FULL_" in m.content]
    folded = [m for m in reads if m.content.startswith("[已折叠]")]
    assert len(full) == 1
    assert "FULL_3" in full[0].content
    assert len(folded) == 2
    # 折叠行保留目标代号，且 read_artifact 自己的配对 tool_call_id 一个不丢
    assert all("grade@g1" in m.content for m in folded)
    assert {m.tool_call_id for m in reads} == {"r1", "r2", "r3"}
    history.close()


@pytest.mark.asyncio
async def test_fetch_folds_after_k_idle_turns_but_refetch_is_full(tmp_path):
    """钉测试②：取回后过 K 轮没再引用折回一行；之后再取仍拿到全文。"""
    history = await new_session(system_prompt="s", model="m", cwd=str(tmp_path), root=tmp_path)
    await _read(history, "a1", {"call_id": "g1"}, "FULL_A")
    for i in range(3):
        await _turn(history, f"问题{i}", f"回答{i}")
    await _read(history, "a2", {"call_id": "g1"}, "FULL_B")

    out, _ = _fold(history, keep=3)
    reads = {m.tool_call_id: m for m in _read_messages(out)}

    assert reads["a1"].content.startswith("[已折叠]")
    assert "FULL_A" not in reads["a1"].content
    assert "grade@g1" in reads["a1"].content
    assert "FULL_B" in reads["a2"].content  # 最近一次取回完整
    history.close()


@pytest.mark.asyncio
async def test_distinct_targets_each_keep_latest_within_window(tmp_path):
    """不同成品（目标键不同）各自保留最近一份，不互相误折叠；无折叠时 changed=False。"""
    history = await new_session(system_prompt="s", model="m", cwd=str(tmp_path), root=tmp_path)
    await _read(history, "x1", {"call_id": "g1"}, "FULL_G1")
    await _read(history, "y1", {"call_id": "g2", "which": "final"}, "FULL_G2")

    out, changed = _fold(history)
    assert changed is False
    reads = _read_messages(out)
    assert len(reads) == 2
    assert all("FULL_" in m.content for m in reads)
    history.close()


@pytest.mark.asyncio
async def test_item_and_which_are_part_of_dedup_key(tmp_path):
    """同篇但 item_id / which 不同算不同目标，各自保留最近完整一份。"""
    history = await new_session(system_prompt="s", model="m", cwd=str(tmp_path), root=tmp_path)
    await _read(history, "p1", {"call_id": "g1", "item_id": "d1"}, "ITEM_D1")
    await _read(history, "p2", {"call_id": "g1", "item_id": "d2"}, "ITEM_D2")
    await _read(history, "p3", {"call_id": "g1", "which": "final"}, "FINAL")

    out, _ = _fold(history)
    reads = _read_messages(out)
    full = [m for m in reads if not m.content.startswith("[已折叠]")]
    assert len(full) == 3  # 三个不同目标键，都在最近窗口 → 全完整
    history.close()


@pytest.mark.asyncio
async def test_error_fetch_is_not_folded(tmp_path):
    """失败的取回（友好报错）很短，不折叠，保留给模型据此换编号。"""
    history = await new_session(system_prompt="s", model="m", cwd=str(tmp_path), root=tmp_path)
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id="e1", name="read_artifact", arguments={"call_id": "zz"})]
        )
    )
    await history.record_tool_result(
        "e1", ToolResult(tool_name="read_artifact", ok=False, output={"error": "没找到 zz"})
    )

    out, changed = _fold(history)
    assert changed is False
    assert _read_messages(out)[0].is_error is True
    history.close()
