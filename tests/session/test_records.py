"""session/records.py 测试：9 类事件的唯一定义 + 信封序列化（R4 单点）。

切片 1：信封五字段与字段顺序。
切片 2：JSON 往返不丢信息（中文、None、嵌套结构都在）。
切片 3：未知类型向前兼容 —— 只被忽略，不让解析炸掉。
切片 4：compact_boundary 只占位，本版没有任何代码会产出它。
"""

from __future__ import annotations

import json
from pathlib import Path

from glyph.session import records
from glyph.session.records import (
    APP_VERSION,
    SCHEMA_VERSION,
    AssistantMessagePayload,
    CancelPayload,
    CompactBoundaryPayload,
    FinalizePayload,
    ReasoningPayload,
    Record,
    RecordType,
    SessionMeta,
    SummaryPayload,
    ToolCallPayload,
    ToolResultPayload,
    TurnEndPayload,
    UserMessagePayload,
    build_record,
)
from glyph.types import FinishReason, Usage

_TS = "2026-09-19T17:50:11.402+08:00"


# ---------- 切片 1：信封 ----------


def test_envelope_has_version_seq_timestamp_type_payload() -> None:
    """一行就是 v / seq / ts / type / payload 五字段，顺序固定便于人肉排查。"""
    record = build_record(
        seq=1, ts=_TS, payload=UserMessagePayload(text="帮我改这句：I has a apple")
    )

    assert record.v == SCHEMA_VERSION == 1
    assert record.seq == 1
    assert record.ts == _TS
    assert record.type == "user_message"
    assert record.payload == {"text": "帮我改这句：I has a apple"}
    assert list(record.model_dump()) == ["v", "seq", "ts", "type", "payload"]


def test_every_record_type_has_exactly_one_payload_model() -> None:
    """每类事件各有且只有一个载荷模型：加类型必须顺手写载荷（词典不能只做一半）。"""
    payload_models = (
        SessionMeta,
        UserMessagePayload,
        AssistantMessagePayload,
        ToolCallPayload,
        ToolResultPayload,
        TurnEndPayload,
        CancelPayload,
        ReasoningPayload,
        CompactBoundaryPayload,
        FinalizePayload,
        SummaryPayload,
    )

    assert len(payload_models) == len(RecordType)
    assert {model.record_type for model in payload_models} == set(RecordType)


def test_session_meta_defaults_to_placeholder_learner_id() -> None:
    """首行 meta：learner_id 第一版恒为 None（预留给一机多人），app_version 跟项目版本。"""
    meta = SessionMeta(
        schema_version=SCHEMA_VERSION,
        session_id="20260919-175003-8f3c2a",
        model="qwen-plus",
        cwd=r"D:\markr",
        created_at=_TS,
        system_prompt_fingerprint="sha256:1a2b3c4d5e6f",
    )

    assert meta.learner_id is None
    assert meta.app_version == APP_VERSION
    assert meta.app_version == "0.1.0"


# ---------- 切片 2：往返 ----------


def test_record_round_trips_through_json() -> None:
    """JSON 一行 → 回来还是同一 objects：中文、None、嵌套的 usage 都不变形。"""
    payload = ToolResultPayload(
        call_id="call_8f21",
        name="grammar_check",
        ok=True,
        duration_ms=1157.0,
        output={"issues": [{"type": "subject_verb", "tip": "I → have"}]},
        error=None,
    )
    original = build_record(seq=4, ts=_TS, payload=payload)

    line = original.to_json_line()
    restored = Record.model_validate_json(line)

    assert restored == original
    assert restored.payload_as(ToolResultPayload) == payload
    # 中文原样落盘，不做 \uXXXX 转义（R9 显式 UTF-8，便于人肉看流水）
    assert json.loads(line)["payload"]["name"] == "grammar_check"
    assert "\n" not in line


def test_turn_end_payload_round_trips_usage_and_stop_reason() -> None:
    """turn_end 携带 usage 与停止原因，也要能原样读回（下来不用再盯着类型是 str 还是枚举）。"""
    payload = TurnEndPayload(
        stop_reason=FinishReason.MAX_ITERATIONS,
        duration_ms=3200.5,
        usage=Usage(input_tokens=120, output_tokens=45),
        iterations=6,
    )
    original = build_record(seq=7, ts=_TS, payload=payload)
    restored = Record.model_validate_json(original.to_json_line())

    assert restored.payload_as(TurnEndPayload) == payload
    restored_payload = restored.payload_as(TurnEndPayload)
    assert restored_payload.stop_reason is FinishReason.MAX_ITERATIONS
    assert restored_payload.usage == Usage(input_tokens=120, output_tokens=45)


# ---------- 切片 3：未知类型 ----------


def test_unknown_record_type_is_ignored_on_read() -> None:
    """将来写了的新类型：老 reader 认不出就说一句 None，由读的一方整行忽略。"""
    raw = json.dumps(
        {"v": 2, "seq": 9, "ts": _TS, "type": "future_summary_v2", "payload": {"whatever": True}},
        ensure_ascii=False,
    )

    record = Record.model_validate_json(raw)

    assert record.type == "future_summary_v2"
    assert record.record_type is None  # 不在词典里 → 读取时整行忽略


def test_cancel_and_reasoning_payloads_are_plain() -> None:
    """cancel / reasoning 的载荷形状固定：都不进模型上下文，但都落盘给人看。"""
    cancel = CancelPayload(reason="user", iteration=3)
    reasoning = ReasoningPayload(text="先确认主语人称，再决定时态。")

    assert cancel.model_dump() == {"reason": "user", "iteration": 3}
    assert reasoning.model_dump() == {"text": "先确认主语人称，再决定时态。"}


# ---------- 切片 4：压缩钩子 ----------


def test_compact_boundary_payload_roundtrip() -> None:
    """compact_boundary 载荷：切点 at_seq + 摘要记录序号；由 history.commit_compaction 产出（#65）。

    records.py 自身只定义类型与载荷、不直接写字面量类型串（产出在 history），所以源文件里
    ``"compact_boundary"`` 字面量仍只在枚举定义处出现一次。
    """
    assert RecordType.COMPACT_BOUNDARY == "compact_boundary"

    boundary = CompactBoundaryPayload(boundary_id="b-1", at_seq=9, summary_record_seq=8)
    assert boundary.model_dump() == {
        "boundary_id": "b-1",
        "at_seq": 9,
        "summary_record_seq": 8,
    }

    record = build_record(seq=10, ts=_TS, payload=boundary)
    assert record.record_type is RecordType.COMPACT_BOUNDARY
    assert record.payload_as(CompactBoundaryPayload).at_seq == 9

    source = Path(records.__file__).read_text(encoding="utf-8")
    assert source.count('"compact_boundary"') == 1


def test_system_prompt_fingerprint_is_stable_and_short() -> None:
    """提示词只留指纹：同一段文字永远同一个值，不同文字不同值，且不存正文。"""
    fingerprint = records.system_prompt_fingerprint("你是一个英语老师")

    assert fingerprint == records.system_prompt_fingerprint("你是一个英语老师")
    assert fingerprint != records.system_prompt_fingerprint("你是一个英语老师！")
    assert len(fingerprint) == len("sha256:") + 12


# ---------- 定稿记录（v2.1 #69 / A1，方案 §8.6） ----------


def test_finalize_payload_record_type_and_roundtrip() -> None:
    """定稿记录是独立 kind=finalize；信封往返后字段一个不丢（中文不转义）。"""
    payload = FinalizePayload(
        grade_call_id="call_abc",
        decisions={"d1": "accepted", "d2": "edited", "d3": "rejected"},
        overrides={"d2": "I have an apple"},
        edited_ids=["d2"],
        kept_ids=["d1", "d2"],
        comment="时态再注意",
        final_text="改后全文：I have an apple.",
    )
    record = build_record(seq=7, ts=_TS, payload=payload)

    assert record.type == "finalize"
    assert record.record_type is RecordType.FINALIZE
    assert FinalizePayload.model_validate(record.payload).final_text == (
        "改后全文：I have an apple."
    )
    # 落盘 JSON 中文不转义，便于人肉审计
    assert "改后全文" in record.to_json_line()


def test_finalize_payload_optional_fields_default_empty() -> None:
    """没有手改 / 评语时，集合字段给空值，端点不必传一堆空壳。"""
    payload = FinalizePayload(
        grade_call_id="call_x",
        decisions={"d1": "accepted"},
        kept_ids=["d1"],
        final_text="全文",
    )
    dumped = payload.model_dump()
    assert dumped["overrides"] == {}
    assert dumped["edited_ids"] == []
    assert dumped["comment"] == ""


# ---------- L3 压缩摘要记录（v2.1 #65，方案 §9.5） ----------


def test_summary_payload_record_type_and_roundtrip() -> None:
    """摘要记录 kind=compact_summary：承载写手产出的摘要正文 + 本次覆盖切点/截断/模型。

    先写摘要、后写 boundary（boundary.summary_record_seq 指回本记录）；信封往返字段不丢、
    中文不转义。
    """
    payload = SummaryPayload(
        text="## 学习者画像\n他在练一般现在时，三单常漏 s。",
        covered_up_to_seq=40,
        material_truncated=True,
        model="qwen3.7-flash",
    )
    record = build_record(seq=41, ts=_TS, payload=payload)

    assert record.type == "compact_summary"
    assert record.record_type is RecordType.COMPACT_SUMMARY
    restored = record.payload_as(SummaryPayload)
    assert restored.text.startswith("## 学习者画像")
    assert restored.covered_up_to_seq == 40
    assert restored.material_truncated is True
    assert restored.model == "qwen3.7-flash"
    assert "三单常漏" in record.to_json_line()


def test_summary_payload_optional_metadata_defaults() -> None:
    """最小摘要只给正文；覆盖切点默认 0、截断默认 False、模型默认空串。"""
    dumped = SummaryPayload(text="摘要正文").model_dump()
    assert dumped == {
        "text": "摘要正文",
        "covered_up_to_seq": 0,
        "material_truncated": False,
        "model": "",
    }
