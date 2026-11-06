"""session/replay.py 测试：把流水回灌成"界面能直接画"的回放序列。

为什么要这一层：界面重开一个历史会话时，只拿 user/assistant 文本的话，
**结构化批改结果（grade_text 的 detections）会整个丢掉**——老师点开旧会话
只看得见 AI 说过的话，看不见它批出来的条目。工具结果本来就在流水里
（ToolResultPayload.output），只是没人把它回灌出来。

切片 1：只回灌三类（user / assistant / tool），其余类型不出现在回放里。
切片 2：tool 项带出工具名、入参、输出——够界面把批改面板重建出来。
切片 3：失败的工具也回灌（ok=False + 人话原因），不静默消失。
切片 4：顺序就是流水原序。
切片 5：store 端到端：写盘再读回来。
切片 6（阶段 2 思考采集）：reasoning（思考）也回灌，出现在对应答复之前。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from glyph.session.records import (
    SCHEMA_VERSION,
    AssistantMessagePayload,
    FinalizePayload,
    ReasoningPayload,
    Record,
    SessionMeta,
    ToolCallPayload,
    ToolResultPayload,
    TurnEndPayload,
    UserMessagePayload,
    build_record,
)
from glyph.session.replay import build_replay, extract_last_input_tokens, extract_session_usage
from glyph.session.store import SessionStore
from glyph.types import Usage

_TZ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 9, 24, 22, 50, 0, tzinfo=_TZ)
_TS = _NOW.isoformat()

_DETECTIONS = [
    {
        "id": "d1",
        "original": "I go to school yesterday",
        "corrected": "I went to school yesterday",
        "type": "hard",
        "tier": "error",
        "source": "model",
        "start": 0,
        "end": 24,
        "aligned": True,
        "explanation_zh": "yesterday 是过去时间，动词要用过去式 went。",
    }
]


def _meta() -> Record:
    return build_record(
        seq=0,
        ts=_TS,
        payload=SessionMeta(
            schema_version=SCHEMA_VERSION,
            session_id="20260924-225000-aaaaaa",
            model="deepseek-v4-pro",
            cwd="D:\\markr",
            created_at=_TS,
            system_prompt_fingerprint="sha256:abc",
        ),
    )


def _user(seq: int, text: str) -> Record:
    return build_record(seq=seq, ts=_TS, payload=UserMessagePayload(text=text))


def _assistant(seq: int, text: str) -> Record:
    return build_record(seq=seq, ts=_TS, payload=AssistantMessagePayload(text=text))


def _tool_call(seq: int, call_id: str, name: str, arguments: dict) -> Record:
    return build_record(
        seq=seq, ts=_TS, payload=ToolCallPayload(id=call_id, name=name, arguments=arguments)
    )


def _tool_result(seq: int, call_id: str, name: str, **kw) -> Record:
    payload = ToolResultPayload(call_id=call_id, name=name, **kw)
    return build_record(seq=seq, ts=_TS, payload=payload)


def _turn_end(seq: int, reason: str = "final_text") -> Record:
    return build_record(
        seq=seq, ts=_TS, payload=TurnEndPayload(stop_reason=reason, duration_ms=1.0)
    )


def _cancel(seq: int, reason: str = "user") -> Record:
    return Record(seq=seq, ts=_TS, type="cancel", payload={"reason": reason})


def _reasoning(seq: int, text: str) -> Record:
    return build_record(seq=seq, ts=_TS, payload=ReasoningPayload(text=text))


# -------- 切片 1：只回灌"给用户看的内容"（user / assistant / tool，阶段 2 含 reasoning） --------


def test_replay_skips_session_meta_and_turn_end() -> None:
    """只有"给用户看的内容"回灌；meta / turn_end 这类过程记录整条跳过。"""
    items = build_replay(
        [
            _meta(),
            _user(1, "帮我改这篇"),
            _turn_end(2),
            _assistant(3, "好的，我看一下"),
        ]
    )
    assert [i.kind for i in items] == ["user", "assistant"]
    assert items[0].text == "帮我改这篇"
    assert items[1].text == "好的，我看一下"


# ---------- 切片 2：tool 项带出重建批改面板所需的全部信息 ----------


def test_tool_item_carries_name_output_and_arguments() -> None:
    """界面要重建批改面板，需要三样：改了哪篇（入参）、批出什么（output）、哪个工具。"""
    items = build_replay(
        [
            _tool_call(1, "c1", "grade_text", {"text": "I go to school yesterday"}),
            _tool_result(
                2,
                "c1",
                "grade_text",
                ok=True,
                output={"detections": _DETECTIONS, "summary": "时态要注意"},
                duration_ms=41850.0,
            ),
        ]
    )
    assert len(items) == 1
    item = items[0]
    assert item.kind == "tool"
    assert item.tool_name == "grade_text"
    assert item.output is not None
    assert item.output["detections"] == _DETECTIONS  # 逐条批改结果原样带出来
    assert item.arguments == {"text": "I go to school yesterday"}  # 原文也在，界面不用再猜
    assert item.duration_ms == 41850.0


def test_tool_item_without_matching_call_is_still_replayed() -> None:
    """工具结果找不到对应的调用（坏行被跳过等）时，照样回灌，只是入参为空。"""
    items = build_replay([_tool_result(1, "c-orphan", "grade_text", ok=True, output={})])
    assert len(items) == 1
    assert items[0].tool_name == "grade_text"
    assert items[0].arguments == {}


# ---------- 切片 3：失败的也回灌 ----------


def test_failed_tool_item_is_replayed_with_error() -> None:
    items = build_replay(
        [_tool_result(1, "c1", "grade_text", ok=False, error="模型返回不是合法 JSON", output=None)]
    )
    assert items[0].ok is False
    assert items[0].error == "模型返回不是合法 JSON"
    assert items[0].output is None  # 失败就是没有输出，不编一个空的


# ---------- 切片 4：顺序 = 流水原序 ----------


def test_replay_preserves_stream_order() -> None:
    items = build_replay(
        [
            _user(1, "改这篇"),
            _tool_call(2, "c1", "grade_text", {"text": "x"}),
            _tool_result(3, "c1", "grade_text", ok=True, output={"detections": []}),
            _assistant(4, "改好了"),
            _tool_call(5, "c2", "recheck_detection", {"text": "x", "round": 1}),
            _tool_result(6, "c2", "recheck_detection", ok=True, output={"status": "upheld"}),
        ]
    )
    assert [i.kind for i in items] == ["user", "tool", "assistant", "tool"]
    assert [i.tool_name for i in items if i.kind == "tool"] == ["grade_text", "recheck_detection"]


# ---------- 切片 6：reasoning 回灌（阶段 2 思考采集） ----------


def test_reasoning_is_replayed_before_the_answer_it_explains() -> None:
    """思考落盘是给历史会话恢复思考块用的：按流水原序回灌，排在对应答复之前。"""
    items = build_replay(
        [
            _user(1, "这题选什么"),
            _reasoning(2, "先判断时态"),
            _assistant(3, "选 B"),
        ]
    )
    assert [i.kind for i in items] == ["user", "reasoning", "assistant"]
    assert items[1].text == "先判断时态"


def test_reasoning_is_not_part_of_model_context() -> None:
    """回灌 ≠ 进模型上下文：reasoning 只是回放条目，不混进任何 assistant 文本。"""
    items = build_replay([_reasoning(1, "内部思考"), _assistant(2, "说人话")])
    assert [i.kind for i in items] == ["reasoning", "assistant"]
    assert items[1].text == "说人话"  # 答复文本不受思考污染


def test_replay_item_is_json_serializable() -> None:
    """回灌结果是给 HTTP 接口用的，必须能 json.dumps 且内容真的在里面。"""
    items = build_replay([_user(1, "hi"), _tool_result(2, "c1", "grade_text", output={"a": 1})])
    dumped = json.dumps([i.model_dump(mode="json") for i in items])
    assert "hi" in dumped
    assert '"a":1' in dumped.replace(" ", "")


# ---------- 切片 5：store 端到端 ----------


@pytest.mark.asyncio
async def test_store_read_replay_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    store = SessionStore(path, clock=lambda: _NOW)
    for rec in (
        _meta(),
        _user(1, "帮我改：I go to school yesterday"),
        _tool_call(2, "c1", "grade_text", {"text": "I go to school yesterday"}),
        _tool_result(3, "c1", "grade_text", ok=True, output={"detections": _DETECTIONS}),
        _assistant(4, "改好了，请看卡片"),
    ):
        await store.append(rec)
    store.close()

    result = SessionStore(path).read_replay()
    assert [i.kind for i in result.items] == ["user", "tool", "assistant"]
    assert result.items[1].output is not None
    assert result.items[1].output["detections"][0]["corrected"] == "I went to school yesterday"
    assert result.skipped_lines == 0


def test_replay_accepts_one_shot_generator() -> None:
    """入参可以是一次性生成器：内部先固化成 list，不能因为扫两遍而静默返回空。"""
    records = (_user(seq, f"第{seq}句") for seq in (1, 2, 3))
    assert [i.text for i in build_replay(records)] == ["第1句", "第2句", "第3句"]


def test_bad_payload_only_drops_itself() -> None:
    """一条脏数据只丢它自己：不能让整个会话读崩（错误即数据）。"""
    bad = Record(
        seq=2, ts=_TS, type="tool_result", payload={"call_id": "c1", "name": "g", "output": "x"}
    )
    items = build_replay([_user(1, "hi"), bad, _assistant(3, "还在")])
    assert [i.kind for i in items] == ["user", "assistant"]


def test_cancelled_run_is_replayed_as_a_stop_status() -> None:
    """取消是用户可见的回合终态：即使这一轮没有 assistant 文本，回放里也要留停止提示。

    思考过程（阶段 2 起）同样回灌给界面画思考块；取消的 status 必须仍在。
    """
    items = build_replay(
        [
            _user(1, "hi"),
            _reasoning(2, "让我想想"),
            _cancel(3),
        ]
    )
    assert [i.kind for i in items] == ["user", "reasoning", "status"]
    assert items[2].status == "cancelled"
    assert items[2].finish_reason == "cancelled"


def test_cancelled_turn_end_is_replayed_as_a_stop_status() -> None:
    """刷新历史后，即使取消回合没有 assistant 文本，也必须保留停止提示。"""
    items = build_replay([_user(1, "继续批改"), _cancel(2), _turn_end(3, "cancelled")])

    assert [i.kind for i in items] == ["user", "status"]
    assert items[1].status == "cancelled"
    assert items[1].finish_reason == "cancelled"


@pytest.mark.asyncio
async def test_store_read_replay_reports_skipped_lines(tmp_path: Path) -> None:
    """回放是给人看历史的通道：丢了几条必须报出来，不能静默。"""
    path = tmp_path / "session.jsonl"
    store = SessionStore(path, clock=lambda: _NOW)
    await store.append(_meta())
    store.close()
    path.write_text(path.read_text(encoding="utf-8") + "{坏行不是json\n", encoding="utf-8")

    assert SessionStore(path).read_replay().skipped_lines == 1


# ---------- 内部 user 消息不进回放（Bug A） ----------


def test_internal_continuation_message_does_not_enter_replay() -> None:
    """Bug A：截断续写的内部指令虽落盘为 user 消息、要给模型看，但不是用户说的话，
    回放给用户看时必须隐藏；它不是坏行，也不应计入 skipped_lines。"""
    from glyph.context import CONTINUATION_PROMPT

    items = build_replay(
        [
            _user(1, "帮我批改这篇长作文……"),
            _assistant(2, "（半截回复）"),
            _user(3, CONTINUATION_PROMPT),
            _assistant(4, "（续写完整回复）"),
        ]
    )

    # 内部 user 整条跳过：只剩 真实 user → assistant(半截) → assistant(续写)
    assert [i.kind for i in items] == ["user", "assistant", "assistant"]
    assert all(CONTINUATION_PROMPT not in i.text for i in items)


# ---------- 切片 6：并行工具的调用顺序（界面刷新后不能和实时不一样） ----------


def test_parallel_tools_carry_call_seq_of_their_declaration() -> None:
    """并行工具倒序跑完时，回放必须带上「被宣告的顺序号」，界面才能按调用顺序画。

    落盘顺序是 调用A → 调用B → 结果B → 结果A（谁先跑完谁先落），
    只看结果的 seq 会把 B 排到 A 前面，和实时看到的先后正好相反。
    """
    items = build_replay(
        [
            _user(1, "改这篇"),
            _tool_call(2, "cA", "grade_text", {"text": "x"}),
            _tool_call(3, "cB", "ocr_image", {"image_id": "i1"}),
            _tool_result(4, "cB", "ocr_image", ok=True, output={"text": "hello"}),
            _tool_result(5, "cA", "grade_text", ok=True, output={"detections": []}),
        ]
    )
    tools = [i for i in items if i.kind == "tool"]
    # 结果按落盘顺序：B 先结果（seq 4），A 后结果（seq 5）
    assert [i.seq for i in tools] == [4, 5]
    # call_seq 记住被宣告的顺序：A 是 2、B 是 3（结果条目的顺序仍是 3、2）
    assert [i.call_seq for i in tools] == [3, 2]
    # 界面按 call_seq 排序后，回到调用顺序
    assert [i.call_id for i in sorted(tools, key=lambda x: x.call_seq or x.seq)] == ["cA", "cB"]


def test_tool_without_matching_call_has_no_call_seq() -> None:
    """找不到对应调用（坏行被跳过等）时 call_seq 为 None，界面退回用 seq，不编一个号。"""
    items = build_replay([_tool_result(1, "c-orphan", "grade_text", ok=True, output={})])
    assert items[0].call_seq is None


# ---------- 切片 7：失败终态在历史里也要看得见 ----------


def test_failed_turn_end_is_replayed_as_a_visible_status() -> None:
    """刷新历史后，「这一轮失败了」必须看得出来，不能显示成一轮空回复。"""
    items = build_replay([_user(1, "改这篇"), _turn_end(2, "failed")])
    assert [i.kind for i in items] == ["user", "status"]
    assert items[1].status == "failed"
    assert items[1].finish_reason == "failed"


def test_normal_turn_end_needs_no_status_item() -> None:
    """正常完成（含触顶总结）不需要额外的状态条目，回放里只有真实内容。"""
    for reason in ("final_text", "max_iterations"):
        items = build_replay([_user(1, "改这篇"), _assistant(2, "改好了"), _turn_end(3, reason)])
        assert [i.kind for i in items] == ["user", "assistant"], reason


# ---------- 定稿区块（v2.1 #69 / A1，方案 §8.6） ----------


def _finalize(seq: int, call_id: str, final_text: str, *, comment: str = "") -> Record:
    return build_record(
        seq=seq,
        ts=_TS,
        payload=FinalizePayload(
            grade_call_id=call_id,
            decisions={"d1": "accepted"},
            kept_ids=["d1"],
            comment=comment,
            final_text=final_text,
        ),
    )


def test_replay_includes_finalize_block() -> None:
    """定稿在回放里是独立 finalize 区块：界面能展示用户最终成果并关联回那次批改。"""
    items = build_replay(
        [
            _meta(),
            _user(1, "改这篇作文"),
            _finalize(2, "call_1", "改后全文", comment="时态不错"),
        ]
    )

    fin = [i for i in items if i.kind == "finalize"]
    assert len(fin) == 1
    block = fin[0]
    assert block.call_id == "call_1"
    assert block.final_text == "改后全文"
    assert block.text == "时态不错"  # 评语复用通用文本字段
    assert block.kept_ids == ["d1"]
    assert block.seq == 2


def test_replay_finalize_shows_only_latest_per_call_id() -> None:
    """同一篇定稿多次：回放只展示最新一版（旧版留在流水里审计，不进用户视图）；
    不同 call_id 的定稿各自保留。"""
    items = build_replay(
        [
            _meta(),
            _finalize(1, "call_a", "A第一版"),
            _finalize(2, "call_b", "B定稿"),
            _finalize(3, "call_a", "A最新版"),
        ]
    )

    fin = [i for i in items if i.kind == "finalize"]
    assert {(i.call_id, i.final_text) for i in fin} == {
        ("call_a", "A最新版"),
        ("call_b", "B定稿"),
    }


# ---------- 切片 7：用量回灌（B-T10-7） ----------


def _turn_end_with_input(seq: int, input_tokens: int | None, reason: str = "final_text") -> Record:
    return build_record(
        seq=seq,
        ts=_TS,
        payload=TurnEndPayload(
            stop_reason=reason, duration_ms=1.0, context_input_tokens=input_tokens
        ),
    )


def _turn_end_with_usage(seq: int, usage) -> Record:
    return build_record(
        seq=seq, ts=_TS, payload=TurnEndPayload(stop_reason="final_text", usage=usage)
    )


def test_extract_session_usage_sums_every_turn() -> None:
    """本会话用量合计：逐轮相加。界面切回旧会话时状态条靠它回显，
    不然刷新一下"本会话省了多少"就没了（用户会以为白省）。"""
    records = [
        _turn_end_with_usage(1, Usage(input_tokens=15033, output_tokens=106, cached_tokens=14336)),
        _turn_end_with_usage(2, Usage(input_tokens=30399, output_tokens=165, cached_tokens=28672)),
    ]

    total = extract_session_usage(records)

    # 逐字段断言（pydantic 忽略未知字段，整对象相等会假绿）
    assert total is not None
    assert total.input_tokens == 45432
    assert total.output_tokens == 271
    assert total.cached_tokens == 43008


def test_extract_session_usage_keeps_cached_unknown_when_never_reported() -> None:
    """一轮都没回缓存明细 → 合计保持"未知"，不兜成 0。"""
    records = [
        _turn_end_with_usage(1, Usage(input_tokens=100, output_tokens=5)),
        _turn_end_with_usage(2, Usage(input_tokens=200, output_tokens=6)),
    ]

    total = extract_session_usage(records)

    assert total is not None
    assert total.input_tokens == 300
    assert total.cached_tokens is None


def test_extract_session_usage_counts_explicit_zero_as_known() -> None:
    """明确回了 0（一个都没命中）→ 合计就是 0，不是未知。"""
    records = [
        _turn_end_with_usage(1, Usage(input_tokens=15000, output_tokens=10, cached_tokens=0)),
    ]

    total = extract_session_usage(records)

    assert total is not None
    assert total.cached_tokens == 0


def test_extract_session_usage_is_none_without_any_usage() -> None:
    """老会话 / 模型从没回用量 → None（界面据此整行不显示，而不是显示 0）。"""
    records = [_turn_end(1), _turn_end(2)]

    assert extract_session_usage(records) is None


def test_extract_last_input_tokens_returns_last_turn_with_usage() -> None:
    """用量环分子 = 最后一个带 context_input_tokens 的 turn_end；中途没回 usage 的不覆盖。"""
    records = [
        _turn_end_with_input(1, 8000),
        _turn_end_with_input(2, 15000),
        _turn_end_with_input(3, None),
    ]
    assert extract_last_input_tokens(records) == 15000


def test_extract_last_input_tokens_none_when_no_usage() -> None:
    """没有任何一轮带 context_input_tokens（老会话 / 模型没回 usage）：回 None，不谎报 0。"""
    assert extract_last_input_tokens([_turn_end(1), _turn_end(2)]) is None


@pytest.mark.asyncio
async def test_store_read_replay_exposes_last_input_tokens(tmp_path: Path) -> None:
    """store 端到端：最后一轮 input tokens 随 ReplayResult 透出，供前端回显用量环。"""
    path = tmp_path / "session.jsonl"
    store = SessionStore(path, clock=lambda: _NOW)
    await store.append(_meta())
    await store.append(_user(1, "改这篇"))
    await store.append(_turn_end_with_input(2, 12345))
    store.close()

    assert SessionStore(path).read_replay().last_input_tokens == 12345
