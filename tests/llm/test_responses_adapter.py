"""千问 Responses 适配层的不联网单测。

切片 1：SSE 帧解析（data:/event:/注释/[DONE]）+ 正文增量流 + completed 用量与结束原因。
切片 2：平台 web_search 三态（in_progress/searching/completed）→ WebSearchDelta，
        且搜索是云端工具，绝不产生本地 ToolCallReady。
切片 3：内部消息 → Responses 请求形状（instructions/input、web_search 工具、max_output_tokens）。
切片 4：客户端 stream_chat（建连重试 / 流中失败 / 截断）。
切片 5：流意外结束（EOF）不得被当成成功——缺 response.completed 一律是可重试错误。
切片 6：意外 EOF 经主循环的真实终态（适配器 + loop 端到端：重发、用尽、取消）。
真 HTTP 冒烟见 test_responses_integration.py。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

import pytest

from glyph.config import Settings
from glyph.llm.base import (
    LLMError,
    ReasoningDelta,
    TextDelta,
    ToolCallReady,
    TurnEnd,
    WebSearchDelta,
)
from glyph.llm.responses import (
    ResponsesLLM,
    iter_sse_events,
    to_responses_input,
    to_responses_tools,
    translate_response_events,
)
from glyph.retry import is_quota_or_auth_error
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.session import new_session
from glyph.tools.base import WriteTool
from glyph.types import (
    AssistantMessage,
    FinishReason,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    UserMessage,
)


def _settings(
    model: str = "deepseek-v4-pro",
    max_tokens: int = 100,
    reasoning_effort: str = "medium",
) -> Settings:
    return Settings(
        api_key="k",
        base_url="https://example.test/v1",
        model=model,
        max_tokens=max_tokens,
        max_tokens_upgrade=999,
        grade_model="g",
        stream_idle_timeout=90.0,
        tool_timeout=120.0,
        reasoning_effort=reasoning_effort,
    )


async def _lines(rows: list[str]) -> AsyncIterator[str]:
    for row in rows:
        await asyncio.sleep(0)
        yield row


async def _dict_events(seq: list[dict]) -> AsyncIterator[dict]:
    for item in seq:
        await asyncio.sleep(0)
        yield item


# ---------- SSE 帧解析 ----------


async def test_iter_sse_events_parses_data_and_skips_framing_lines() -> None:
    rows = [
        "id:1",
        ":HTTP_STATUS/200",
        "event: response.created",
        'data: {"type":"response.created"}',
        "",
        'data: {"type":"response.output_text.delta","delta":"你"}',
        "data: [DONE]",
        'data: {"type":"should_not_reach"}',
    ]

    events = [event async for event in iter_sse_events(_lines(rows))]

    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_text.delta",
    ]


async def test_iter_sse_events_ignores_blank_and_malformed_data() -> None:
    rows = [
        "data: ",
        "data: {not valid json}",
        'data: {"type":"response.created"}',
    ]

    events = [event async for event in iter_sse_events(_lines(rows))]

    assert [event["type"] for event in events] == ["response.created"]


# ---------- 正文流 + 结束 ----------


async def test_plain_text_stream_collects_deltas_and_completed_usage() -> None:
    seq = [
        {"type": "response.created"},
        {"type": "response.in_progress"},
        {"type": "response.content_part.added"},
        {"type": "response.output_text.delta", "delta": "12"},
        {"type": "response.output_text.delta", "delta": "月12日"},
        {"type": "response.output_text.done"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    deltas = [event for event in events if isinstance(event, TextDelta)]
    assert "".join(event.delta for event in deltas) == "12月12日"

    end = [event for event in events if isinstance(event, TurnEnd)][0]
    assert end.stop_reason == "stop"
    assert end.usage is not None
    assert end.usage.input_tokens == 100
    assert end.usage.output_tokens == 20


async def test_reasoning_and_framing_events_do_not_emit_text() -> None:
    # 千问思考事件(reasoning_text)与 item 生命周期事件都不应被当成正文
    seq = [
        {"type": "response.reasoning_text.delta", "delta": "我需要搜索一下"},
        {"type": "response.output_item.added", "item": {"type": "reasoning"}},
        {"type": "response.output_text.delta", "delta": "答案"},
        {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    text = "".join(event.delta for event in events if isinstance(event, TextDelta))
    assert text == "答案"


async def test_reasoning_text_delta_is_surfaced_as_reasoning_delta() -> None:
    """思考走 response.reasoning_text.delta（实测事件名，带 response. 前缀）。

    2026-10-12 用户真机确认主脑会吐思考文本；适配层此前整类忽略，思考全丢。
    这次接住并如实上报，但仍**只展示**：不进 blocks、不写账本。
    """
    seq = [
        {"type": "response.reasoning_text.delta", "delta": "我需要先"},
        {"type": "response.reasoning_text.delta", "delta": "看看这句"},
        {"type": "response.output_text.delta", "delta": "好"},
        {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    think = "".join(e.delta for e in events if isinstance(e, ReasoningDelta))
    assert think == "我需要先看看这句"
    # 两条流不许相混
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "好"


async def test_stream_without_reasoning_events_emits_no_reasoning_delta() -> None:
    """不吐思考的轮次：一条 ReasoningDelta 都不许发（空思考不占位）。"""
    seq = [
        {"type": "response.output_text.delta", "delta": "答案"},
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    assert [e for e in events if isinstance(e, ReasoningDelta)] == []


async def test_empty_reasoning_delta_is_not_emitted() -> None:
    """空串思考帧不许上屏（有些平台会用空 delta 表示"思考段落结束"）。"""
    seq = [
        {"type": "response.reasoning_text.delta", "delta": ""},
        {"type": "response.output_text.delta", "delta": "答案"},
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    assert [e for e in events if isinstance(e, ReasoningDelta)] == []
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "答案"


# ---------- 切片 2：平台 web_search 三态 ----------


def _web_search_phase(phase: str) -> dict:
    # 真实流式里三个 web_search_call.* 事件的 item 是空壳，不带 action
    return {"type": f"response.web_search_call.{phase}", "item": {}}


def _search_item_done(action: dict) -> dict:
    # 搜索词 / 来源在 output_item.done 聚合完成的 item.action 上才带下来
    return {
        "type": "response.output_item.done",
        "item": {"type": "web_search_call", "status": "completed", "action": action},
    }


def _completed_usage(extra: dict | None = None) -> dict:
    usage = {"input_tokens": 9, "output_tokens": 1}
    if extra:
        usage.update(extra)
    return {"type": "response.completed", "response": {"status": "completed", "usage": usage}}


async def test_web_search_three_phases_emit_query_and_sources() -> None:
    seq = [
        _web_search_phase("in_progress"),
        _web_search_phase("searching"),
        _web_search_phase("completed"),  # 空壳事件，不产出内部事件
        _search_item_done(
            {
                "query": "六级 日期",
                "sources": [
                    {"type": "url", "url": "https://a.example"},
                    {"type": "url", "title": "标题", "url": "https://b.example"},
                ],
            }
        ),
        # 正文 message 的 output_item.done 不能被误当成搜索结果
        {"type": "response.output_item.done", "item": {"type": "message"}},
        {"type": "response.output_text.delta", "delta": "答案"},
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    searches = [event for event in events if isinstance(event, WebSearchDelta)]
    assert [event.phase for event in searches] == ["in_progress", "searching", "completed"]
    assert searches[1].query is None  # searching 空壳拿不到搜索词

    completed = searches[2]  # 来自 output_item.done
    assert completed.query == "六级 日期"
    assert [source.url for source in completed.sources] == [
        "https://a.example",
        "https://b.example",
    ]
    assert completed.sources[0].title is None  # 平台常常只给 URL、不给标题
    assert completed.sources[1].title == "标题"

    # 平台搜索不是本地 function：绝不产出 ToolCallReady
    assert not any(isinstance(event, ToolCallReady) for event in events)


async def test_parallel_queries_falls_back_to_first_query() -> None:
    # qwen 一次搜索会在 queries 里并行发多个词；没有单数 query 时取第一个。
    # 但这次没有任何来源：phase 必须是 no_result（不能报 completed 成功）
    seq = [
        _search_item_done({"queries": ["q1", "q2"], "sources": []}),
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    search = [event for event in events if isinstance(event, WebSearchDelta)][0]
    assert search.phase == "no_result"
    assert search.query == "q1"
    assert search.sources == []


async def test_completed_search_without_sources_is_no_result() -> None:
    # 搜索流程走完、平台却没给任何来源：明确 no_result，让前端显失败、模型不许据此编造
    seq = [
        _search_item_done({"query": "时效问题", "sources": []}),
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    search = [event for event in events if isinstance(event, WebSearchDelta)][0]
    assert search.phase == "no_result"
    assert search.query == "时效问题"
    assert search.sources == []


async def test_completed_search_with_sources_stays_completed() -> None:
    seq = [
        _search_item_done({"query": "q", "sources": [{"type": "url", "url": "https://x.example"}]}),
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    search = [event for event in events if isinstance(event, WebSearchDelta)][0]
    assert search.phase == "completed"
    assert search.sources[0].url == "https://x.example"


async def test_in_progress_search_has_no_query_nor_sources() -> None:
    seq = [_web_search_phase("in_progress"), _completed_usage()]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    search = [event for event in events if isinstance(event, WebSearchDelta)][0]
    assert search.query is None
    assert search.sources == []


async def test_multiple_independent_searches_each_complete_and_count_sums() -> None:
    # 平台 agent 自主发起两次独立检索：两组三态空壳 + 两个 output_item.done，各带来源
    seq = [
        _web_search_phase("in_progress"),
        _web_search_phase("searching"),
        _web_search_phase("completed"),
        _search_item_done({"query": "q1", "sources": [{"type": "url", "url": "https://1"}]}),
        _web_search_phase("in_progress"),
        _web_search_phase("searching"),
        _web_search_phase("completed"),
        _search_item_done({"query": "q2", "sources": [{"type": "url", "url": "https://2"}]}),
        {"type": "response.output_text.delta", "delta": "答案"},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "x_tools": {"web_search": {"count": 2}},
                },
            },
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    searches = [event for event in events if isinstance(event, WebSearchDelta)]
    assert [event.phase for event in searches] == [
        "in_progress",
        "searching",
        "completed",
        "in_progress",
        "searching",
        "completed",
    ]
    completed = [event for event in searches if event.phase == "completed"]
    assert [event.query for event in completed] == ["q1", "q2"]
    assert [event.sources[0].url for event in completed] == ["https://1", "https://2"]

    end = [event for event in events if isinstance(event, TurnEnd)][0]
    assert end.web_search_count == 2


# ---------- 切片 3：请求映射 ----------


async def test_to_responses_input_maps_system_to_instructions_and_turns_to_items() -> None:
    messages = [
        SystemMessage(content="sys"),
        UserMessage(content="hi"),
        AssistantMessage(blocks=[TextBlock(text="yo")]),
        UserMessage(content="q2"),
    ]

    instructions, items = to_responses_input(messages)

    assert instructions == "sys"
    assert items == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "yo"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "q2"}]},
    ]


async def test_to_responses_input_without_system_has_none_instructions() -> None:
    instructions, items = to_responses_input([UserMessage(content="hi")])
    assert instructions is None
    assert items[0]["role"] == "user"


async def test_to_responses_input_maps_local_tool_artifacts_phase2() -> None:
    # 阶段 2：本地 function 调用映射成 function_call，结果映射成 function_call_output，
    # call_id 严格配对；arguments 序列化成 JSON 字符串
    _, items = to_responses_input(
        [
            AssistantMessage(
                blocks=[ToolCallBlock(id="call_1", name="grade_text", arguments={"text": "a b"})]
            ),
            ToolMessage(tool_call_id="call_1", name="grade_text", content='{"ok":true}'),
        ]
    )
    assert items[0] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "grade_text",
        "arguments": json.dumps({"text": "a b"}, ensure_ascii=False),
    }
    assert items[1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok":true}',
    }


def test_build_payload_declares_web_search_and_max_output_tokens() -> None:
    llm = ResponsesLLM(_settings(max_tokens=100), client=object(), web_search=True)
    messages = [SystemMessage(content="sys"), UserMessage(content="hi")]

    payload = llm.build_payload(messages, max_tokens=None)

    assert payload["model"] == "deepseek-v4-pro"
    assert payload["stream"] is True
    assert payload["max_output_tokens"] == 100  # Responses 参数名（不是 max_tokens）
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["instructions"] == "sys"
    assert payload["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]


def test_build_payload_respects_override_and_disables_search() -> None:
    llm = ResponsesLLM(_settings(), client=object(), web_search=False)
    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=512)

    assert payload["tools"] == []
    assert payload["max_output_tokens"] == 512
    assert "instructions" not in payload


def test_build_payload_injects_reasoning_effort_for_supported_brain() -> None:
    # 菜单内、实测支持思考的大脑：用户选的"高"档必须作为顶层 reasoning.effort 发出
    llm = ResponsesLLM(
        _settings(model="qwen3.8-max", reasoning_effort="high"),
        client=object(),
        web_search=False,
    )
    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=100)

    assert payload["reasoning"] == {"effort": "high"}


def test_build_payload_reasoning_reflects_low_level() -> None:
    llm = ResponsesLLM(
        _settings(model="glm-5.3", reasoning_effort="low"),
        client=object(),
        web_search=False,
    )
    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=100)

    assert payload["reasoning"] == {"effort": "low"}


def test_build_payload_omits_reasoning_for_brain_not_in_capability_table() -> None:
    # 不在大脑能力表里的型号（后端默认 / .env 旧型号，未实测思考档）：
    # 不发 reasoning，维持模型自身默认 —— 绝不把档位发给没验证过的型号导致 400
    llm = ResponsesLLM(
        _settings(model="deepseek-v4-pro", reasoning_effort="high"),
        client=object(),
        web_search=False,
    )
    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=100)

    assert "reasoning" not in payload


def test_build_payload_model_override_switches_model_and_capability() -> None:
    # 主脑额度兜底：model 覆写不仅要换 model 名，思考档等能力位也要按"覆写后的脑"重算。
    # 首选 deepseek-v4-pro（能力表外、不发 reasoning），覆写到支持 high 的 qwen3.8-max。
    llm = ResponsesLLM(
        _settings(model="deepseek-v4-pro", reasoning_effort="high"),
        client=object(),
        web_search=False,
    )
    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=100, model="qwen3.8-max")

    assert payload["model"] == "qwen3.8-max"
    assert payload["reasoning"] == {"effort": "high"}


def test_build_payload_without_model_override_keeps_bound_model() -> None:
    llm = ResponsesLLM(_settings(model="kimi-k3"), client=object(), web_search=False)
    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=100)

    assert payload["model"] == "kimi-k3"


def test_to_responses_tools_flattens_chat_schemas_and_prepends_search() -> None:
    chat = [
        {
            "type": "function",
            "function": {
                "name": "grade_text",
                "description": "grade",
                "parameters": {"type": "object"},
            },
        }
    ]
    assert to_responses_tools(chat, web_search=True) == [
        {"type": "web_search"},
        {
            "type": "function",
            "name": "grade_text",
            "description": "grade",
            "parameters": {"type": "object"},
        },
    ]
    assert to_responses_tools(chat, web_search=False)[0]["type"] == "function"
    assert to_responses_tools(None, web_search=False) == []


def test_to_responses_tools_fills_empty_required_for_optional_only_params() -> None:
    # DashScope 兼容 /responses 端点校验器要求每个 function 的 parameters
    # 必须带 required 键（空数组也行），缺失直接 400；全可选参数的工具
    # （get_quiz/read_attempts/read_artifact）最容易踩。
    chat = [
        {
            "type": "function",
            "function": {
                "name": "get_quiz",
                "description": "取题",
                "parameters": {
                    "type": "object",
                    "properties": {"exam": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_memory",
                "description": "读记忆",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "query": {"type": "string"}},
                    "anyOf": [{"required": ["key"]}, {"required": ["query"]}],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lookup_word",
                "description": "查词",
                "parameters": {
                    "type": "object",
                    "properties": {"word": {"type": "string"}},
                    "required": ["word"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    out = {t["name"]: t for t in to_responses_tools(chat, web_search=False)}
    # 缺 required 的补上空数组，其余字段（含 anyOf）原样保留
    assert out["get_quiz"]["parameters"]["required"] == []
    assert out["get_quiz"]["parameters"]["properties"] == {"exam": {"type": "string"}}
    assert out["read_memory"]["parameters"]["required"] == []
    assert out["read_memory"]["parameters"]["anyOf"] == [
        {"required": ["key"]},
        {"required": ["query"]},
    ]
    # 已有 required 的原样不动，绝不覆盖
    assert out["lookup_word"]["parameters"]["required"] == ["word"]


async def test_function_call_item_emits_tool_call_ready_and_tool_calls_reason() -> None:
    seq = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "grade_text",
                "arguments": '{"text": "a b"}',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        },
    ]
    events = [event async for event in translate_response_events(_dict_events(seq))]

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].call.id == "call_1"
    assert ready[0].call.name == "grade_text"
    assert ready[0].call.arguments == {"text": "a b"}

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "tool_calls"


async def test_response_failed_quota_error_is_not_retryable() -> None:
    # 真实样本：code=server_error 但 message 是免费额度耗尽 → 不充值重试无意义
    seq = [
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {
                    "code": "server_error",
                    "message": (
                        "Free quota exhausted. To continue, please add funds or "
                        'disable the "use free tier only" mode in the console.'
                    ),
                },
            },
        }
    ]
    with pytest.raises(LLMError) as exc:
        [event async for event in translate_response_events(_dict_events(seq))]
    assert exc.value.retryable is False


async def test_response_failed_transient_server_error_is_retryable() -> None:
    # 同样是 server_error，message 是临时过载（无配额/欠费字样）→ 可重试
    seq = [
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {"code": "server_error", "message": "Service temporarily overloaded."},
            },
        }
    ]
    with pytest.raises(LLMError) as exc:
        [event async for event in translate_response_events(_dict_events(seq))]
    assert exc.value.retryable is True


async def test_completed_usage_carries_cached_tokens() -> None:
    """Responses 通道的"命中缓存"在 usage.input_tokens_details.cached_tokens 里，
    与 Chat 通道字段名不同、语义一样；界面两处只有一份渲染。"""
    seq = [
        _delta_event("好"),
        _completed_usage({"input_tokens_details": {"cached_tokens": 7168}}),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]
    end = [e for e in events if isinstance(e, TurnEnd)][0]

    assert end.usage.cached_tokens == 7168


async def test_completed_usage_without_details_stays_unknown() -> None:
    seq = [_delta_event("好"), _completed_usage()]

    events = [event async for event in translate_response_events(_dict_events(seq))]
    end = [e for e in events if isinstance(e, TurnEnd)][0]

    assert end.usage.cached_tokens is None


async def test_response_failed_invalid_key_is_classified_as_auth() -> None:
    """钥匙不对（平台回 invalid_api_key）与额度耗尽同一类：都是"这个模型用不了"，
    必须能触发换脑，而不是把英文原文甩给用户。

    文案形态与上面的额度样本不同（Invalid API-key / InvalidApiKey）——少一个空格或者
    把空格换成连字符就判不出来，正是 2026-10-12 那次 401 的根子。
    """
    seq = [
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {
                    "code": "invalid_api_key",
                    "message": "Invalid API-key provided.",
                },
            },
        }
    ]
    with pytest.raises(LLMError) as exc:
        [event async for event in translate_response_events(_dict_events(seq))]
    assert is_quota_or_auth_error(exc.value) is True


# ---------- 切片 4：客户端 stream_chat（建连重试 / 流中失败 / 截断） ----------


def _simple_sse(*, text: str = "你好") -> list[str]:
    """一条正文增量 + 一条整轮终态的 SSE 行。

    早期这里是手工拼的 f-string，多了一个 `}`，产出的是**坏 JSON**，被
    ``iter_sse_events`` 按"坏帧跳过"静默丢弃——于是这两个用例实际喂的是"没有终态
    的流"，却因为旧实现把 EOF 当成功而一直是绿的（与 BUG-1 同类的"离线数据照着
    错误假设造"）。改用 json.dumps 生成，保证喂进去的真是合法终态帧。
    """
    return [
        "event: response.output_text.delta",
        f"data: {json.dumps({'type': 'response.output_text.delta', 'delta': text})}",
        f"data: {json.dumps(_completed_usage())}",
    ]


class FakeStreamingResponse:
    """模拟 with_streaming_response.create() 返回的异步上下文管理器（AsyncAPIResponse）。"""

    def __init__(
        self,
        lines: list[str] | None = None,
        *,
        enter_error: Exception | None = None,
        line_error: Exception | None = None,
    ) -> None:
        self._lines = lines or []
        self._enter_error = enter_error
        self._line_error = line_error
        self.status_code = 200
        self.closed = False

    async def __aenter__(self) -> FakeStreamingResponse:
        if self._enter_error is not None:
            raise self._enter_error
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.closed = True
        return False

    async def iter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)
            yield line
        if self._line_error is not None:
            raise self._line_error


class FakeResponsesClient:
    """每次 create 返回队列里的下一个异步上下文管理器。"""

    def __init__(self, responses: list[FakeStreamingResponse]) -> None:
        self._queue = responses
        self.calls: list[dict] = []

        # 同步方法返回异步上下文管理器，对齐 SDK: async with client...create(...) as resp
        def create(**payload):
            self.calls.append(payload)
            return self._queue.pop(0)

        self.responses = type(
            "R",
            (),
            {"with_streaming_response": type("W", (), {"create": staticmethod(create)})()},
        )()


def _conn_error() -> Exception:
    # 复用适配层 import 的同一个 openai，构造一个可重试的连接错误
    from glyph.llm import responses as responses_mod

    return responses_mod.openai.APIConnectionError(request=None)


async def _collect_stream(llm: ResponsesLLM) -> list[object]:
    return [event async for event in llm.stream_chat([UserMessage(content="hi")], tools=[])]


async def test_stream_chat_yields_events_and_closes_response() -> None:
    response = FakeStreamingResponse(_simple_sse())
    client = FakeResponsesClient([response])
    llm = ResponsesLLM(_settings(), client=client)

    events = await _collect_stream(llm)

    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "你好"
    assert any(isinstance(e, TurnEnd) for e in events)
    assert len(client.calls) == 1  # 一次建连
    assert response.closed  # 流结束关闭了底层响应


async def test_stream_chat_retries_retryable_connection_error_then_succeeds() -> None:
    bad = FakeStreamingResponse(enter_error=_conn_error())
    good = FakeStreamingResponse(_simple_sse())
    client = FakeResponsesClient([bad, good])
    llm = ResponsesLLM(_settings(), client=client, max_retries=2, backoff_base=0)

    events = await _collect_stream(llm)

    assert any(isinstance(e, TurnEnd) for e in events)
    assert len(client.calls) == 2  # 第一次建连失败 → 重新建连
    assert good.closed


async def test_stream_chat_does_not_retry_non_retryable_error() -> None:
    bad = FakeStreamingResponse(enter_error=ValueError("400 bad request"))
    client = FakeResponsesClient([bad])
    llm = ResponsesLLM(_settings(), client=client, max_retries=3, backoff_base=0)

    from glyph.llm.base import LLMError

    with pytest.raises(LLMError) as exc_info:
        await _collect_stream(llm)
    assert exc_info.value.retryable is False
    assert len(client.calls) == 1  # 4xx/未知错误不重试


async def test_stream_chat_gives_up_after_retries_exhausted() -> None:
    client = FakeResponsesClient(
        [FakeStreamingResponse(enter_error=_conn_error()) for _ in range(3)]
    )
    llm = ResponsesLLM(_settings(), client=client, max_retries=2, backoff_base=0)

    from glyph.llm.base import LLMError

    with pytest.raises(LLMError) as exc_info:
        await _collect_stream(llm)
    assert exc_info.value.retryable is True
    assert len(client.calls) == 3  # 首次 + 2 次重试


async def test_stream_chat_error_mid_stream_is_wrapped_but_not_retried() -> None:
    # 流已经开始后断网：不重新建连（请求可能已在服务端执行、token 照花），
    # 包成可重试 LLMError 交给主循环决定是否整轮重发。
    response = FakeStreamingResponse(_simple_sse(text="半截"), line_error=_conn_error())
    client = FakeResponsesClient([response])
    llm = ResponsesLLM(_settings(), client=client, max_retries=3, backoff_base=0)

    from glyph.llm.base import LLMError

    with pytest.raises(LLMError) as exc_info:
        await _collect_stream(llm)
    assert exc_info.value.retryable is True
    assert len(client.calls) == 1  # 流中断不在这里重试


async def test_incomplete_status_maps_to_length() -> None:
    seq = [
        {
            "type": "response.completed",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 5, "output_tokens": 100},
            },
        }
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    end = [event for event in events if isinstance(event, TurnEnd)][0]
    assert end.stop_reason == "length"


# ---------- 本轮修复回归用例 ----------


async def test_multiple_parallel_function_calls_in_one_turn() -> None:
    # 模型同一回合自主并行调两个本地 function：两个 output_item.done 各成一条 ToolCallReady
    seq = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_a",
                "name": "lookup_word",
                "arguments": '{"word": "hello"}',
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_b",
                "name": "read_memory",
                "arguments": "{}",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert [(r.call.id, r.call.name) for r in ready] == [
        ("call_a", "lookup_word"),
        ("call_b", "read_memory"),
    ]
    assert ready[0].call.arguments == {"word": "hello"}
    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "tool_calls"


async def test_function_call_broken_json_arguments_default_to_empty_dict() -> None:
    # 半截 arguments（流被截断 / 模型吐坏）：解析失败安全退成 {}，不抛、不把字符串当 dict
    seq = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_a",
                "name": "grade_text",
                "arguments": '{"text": "半截',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]
    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert ready[0].call.arguments == {}


async def test_function_call_non_object_arguments_wrapped_under_value() -> None:
    # 合法 JSON 但不是对象（数字 / 数组）：兜进 {"value": ...}，保证 arguments 始终是 dict
    seq = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_a",
                "name": "grade_text",
                "arguments": "123",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]
    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert ready[0].call.arguments == {"value": 123}


async def test_function_call_without_call_id_uses_synthetic_instead_of_msg_id() -> None:
    # item 只有输出项 id（msg_）、缺 call_id：合成 call_{name}，绝不拿 msg_ id 冒名配对
    seq = [
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "msg_xyz",
                "name": "grade_text",
                "arguments": "{}",
            },
        },
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        },
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]
    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert ready[0].call.id == "call_grade_text"
    assert ready[0].call.id != "msg_xyz"


async def test_response_failed_without_error_body_defaults_retryable() -> None:
    # response.failed 但没带 error 体：给默认说明，按可重试处理（可能是临时故障）
    seq = [{"type": "response.failed", "response": {"status": "failed"}}]

    with pytest.raises(LLMError) as exc:
        [event async for event in translate_response_events(_dict_events(seq))]
    assert exc.value.retryable is True


async def test_web_search_started_and_completed_carry_search_id() -> None:
    # output_item.added 给 started + search_id；output_item.done 的 item.id 即同一 search_id
    seq = [
        {
            "type": "response.output_item.added",
            "item": {"type": "web_search_call", "id": "ws_abc", "status": "in_progress"},
        },
        _web_search_phase("in_progress"),  # 空壳进度帧，无 id
        {
            "type": "response.output_item.done",
            "item": {
                "type": "web_search_call",
                "id": "ws_abc",
                "status": "completed",
                "action": {
                    "query": "q",
                    "sources": [{"type": "url", "url": "https://x.example"}],
                },
            },
        },
        _completed_usage(),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    searches = [e for e in events if isinstance(e, WebSearchDelta)]
    started = [e for e in searches if e.phase == "started"]
    assert started[0].search_id == "ws_abc"
    completed = [e for e in searches if e.phase == "completed"]
    assert completed[0].search_id == "ws_abc"
    assert completed[0].query == "q"


def _chat_function_schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "d",
            "parameters": {"type": "object"},
        },
    }


def test_build_payload_web_search_off_means_no_cloud_search() -> None:
    # 通道开关关掉时，就算有本地工具也不该出现云端搜索
    llm = ResponsesLLM(_settings(), client=object(), web_search=False)
    payload = llm.build_payload(
        [UserMessage(content="hi")],
        max_tokens=None,
        tools=[_chat_function_schema("grade_text")],
    )
    assert [t["type"] for t in payload["tools"]] == ["function"]


# ⚠️ 旧用例 test_build_payload_empty_tools_drops_cloud_web_search 已按 T02 改掉：
# 它断言的是"显式 tools=[] == 摘掉全部工具"，而这恰恰把"纯搜索会话（本来就没本地工具）"
# 也一起关掉了搜索 —— 与 demo / 搜索 integration 的意图冲突。摘工具现在由 strip_tools
# 显式表达，见切片 8 的 test_build_payload_strip_tools_disables_everything。


def test_build_payload_nonempty_tools_prepends_cloud_web_search() -> None:
    llm = ResponsesLLM(_settings(), client=object(), web_search=True)
    payload = llm.build_payload(
        [UserMessage(content="hi")],
        max_tokens=None,
        tools=[_chat_function_schema("grade_text")],
    )
    assert payload["tools"][0] == {"type": "web_search"}
    assert payload["tools"][1]["name"] == "grade_text"


# ---------- 切片 5：流意外结束（EOF）不得被当成成功 ----------
#
# 背景（T01）：Responses 流只有收到 response.completed 才算"这一轮真的结束了"。
# 连接被掐断 / 服务端半途崩掉时，SSE 会在没有任何终态事件的情况下静默到尾；
# 旧实现初始 stop_reason="stop"、迭代器结束就直接吐 TurnEnd，于是半截回复被当成
# 完整消息入账，用户看到的是"回答完了"，实际内容缺了一大段。


async def _drive_until_error(seq: list[dict]) -> tuple[list[object], LLMError]:
    """跑一条事件流直到它抛错，返回 (已产出的事件, 抛出的 LLMError)。"""
    events: list[object] = []
    with pytest.raises(LLMError) as exc_info:
        async for event in translate_response_events(_dict_events(seq)):
            events.append(event)
    return events, exc_info.value


async def test_eof_after_text_delta_is_error_not_successful_turn_end() -> None:
    # 最小复现：只给一条文本增量，流就无声结束了（没有 response.completed）
    events, error = await _drive_until_error(
        [
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "半截答案"},
        ]
    )

    # 半截内容确实上屏了（UI 靠随后的 turn_retry 回退），但绝没有正常终态
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "半截答案"
    assert not any(isinstance(e, TurnEnd) for e in events)
    assert error.retryable is True  # 可恢复故障，交主循环整轮重发


async def test_empty_stream_without_terminal_event_is_error() -> None:
    # 一次事件都没收到就结束（建连后立刻断）：不能产出"空的正常结束"
    events, error = await _drive_until_error([])

    assert events == []
    assert error.retryable is True


async def test_function_call_done_without_turn_terminal_is_error() -> None:
    # 函数调用项已经 done，但整轮终态没来：不能当成"工具调用完成"（缺用量/结束原因）
    events, error = await _drive_until_error(
        [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "grade_text",
                    "arguments": '{"text": "a b"}',
                },
            },
        ]
    )

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1  # 调用确实产出了，但这一轮不算成功
    assert not any(isinstance(e, TurnEnd) for e in events)
    assert error.retryable is True


async def test_stream_chat_unexpected_eof_is_retryable_error_and_closes() -> None:
    # 客户端层：SSE 半途断掉 → 可重试 LLMError（复用主循环既有重发路径），且关掉底层响应
    response = FakeStreamingResponse(['data: {"type":"response.output_text.delta","delta":"半截"}'])
    client = FakeResponsesClient([response])
    llm = ResponsesLLM(_settings(), client=client)

    with pytest.raises(LLMError) as exc_info:
        await _collect_stream(llm)

    assert exc_info.value.retryable is True
    assert len(client.calls) == 1  # 流中断不在这里重试（同"流中失败"既有策略）
    assert response.closed  # 出错也要关掉底层响应，别漏连接


# ---------- 切片 6：意外 EOF 经主循环的真实终态（适配器 + loop 端到端） ----------


def _sse(*events: dict) -> list[str]:
    """把事件 dict 编成 SSE data 行（模拟千问的一帧一行）。"""
    return [f"data: {json.dumps(event)}" for event in events]


def _delta_event(text: str) -> dict:
    return {"type": "response.output_text.delta", "delta": text}


def _function_call_event(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        },
    }


class _NoteWriteTool(WriteTool):
    """有副作用的写入工具：只记录被真正执行了几次（验证重发不会重复执行）。"""

    name = "note"
    description = "记一条笔记"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, args: dict) -> dict:
        self.calls.append(args)
        return {"ok": True}


async def _new_history(tmp_path) -> object:
    """开一个真会话（落到 tmp_path 的 JSONL），让"半截不入账"能被真账本证明。"""
    return await new_session(
        system_prompt="你是测试助手。",
        model="test-model",
        cwd=str(tmp_path),
        root=tmp_path,
    )


def _eof_after_half_turn() -> list[str]:
    """一条"半截流"：说了半句 + 一个工具调用，然后没有任何终态事件就结束。"""
    return _sse(_delta_event("半截"), _function_call_event("call_1", "note"))


async def test_loop_retries_eof_turn_then_succeeds_without_double_writing(tmp_path) -> None:
    # 第一次：说了半句 + 一个工具调用，流就断了；第二次：完整拿到工具调用终态
    # 第三次：工具结果回灌后给出最终答复
    tool = _NoteWriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool_call = _function_call_event("call_1", "note", '{"text": "过去时"}')
    client = FakeResponsesClient(
        [
            FakeStreamingResponse(_eof_after_half_turn()),
            FakeStreamingResponse(_sse(tool_call, _completed_usage())),
            FakeStreamingResponse(_sse(_delta_event("记好了"), _completed_usage())),
        ]
    )
    llm = ResponsesLLM(_settings(), client=client)
    history = await _new_history(tmp_path)
    loop = AgentLoop(llm, registry, history=history, max_stream_retries=2, retry_backoff_base=0.0)

    try:
        events = [event async for event in loop.run("帮我记一下")]

        # 断掉的那一轮走了既有重发路径（不是成功、也不是直接失败）
        retries = [e for e in events if e.type == "turn_retry"]
        assert len(retries) == 1
        assert retries[0].reason == "network"

        # 写入工具只在重发成功后的那一轮执行一次：半截那次的调用没有被执行过
        assert len(tool.calls) == 1

        result = events[-1]
        assert result.finish_reason == FinishReason.FINAL_TEXT
        assert result.text == "记好了"  # 半截"半截答案"没有混进最终答复

        # 账本：工具调用与结果严格配对；半截 assistant 从未入账
        messages = history.messages()
        assert [m.role for m in messages] == [
            "system",
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        # 配对要看 call_id，不能只看 role 序列：结果必须挂在被执行的那个调用上
        called = messages[2].tool_calls
        assert [call.id for call in called] == ["call_1"]
        assert messages[3].tool_call_id == called[0].id
        assert messages[2].text == ""  # 半截那次说的话没有跟着入账
    finally:
        history.close()


async def test_loop_eof_retries_exhausted_fails_without_executing_tool(tmp_path) -> None:
    # 每次都断在同一个地方：重发预算用尽 → error + failed，工具一次都不执行
    tool = _NoteWriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    client = FakeResponsesClient(
        [
            FakeStreamingResponse(_eof_after_half_turn()),
            FakeStreamingResponse(_eof_after_half_turn()),
        ]
    )
    llm = ResponsesLLM(_settings(), client=client)
    history = await _new_history(tmp_path)
    loop = AgentLoop(llm, registry, history=history, max_stream_retries=1, retry_backoff_base=0.0)

    try:
        events = [event async for event in loop.run("帮我记一下")]

        assert [e.attempt for e in events if e.type == "turn_retry"] == [1]
        assert any(e.type == "error" for e in events)
        assert events[-1].finish_reason == FinishReason.FAILED
        assert tool.calls == []  # 半截轮里的工具调用不执行，更不会重复执行
        assert [m.role for m in history.messages()] == ["system", "user"]
    finally:
        history.close()


async def test_cancel_during_eof_retry_backoff_still_ends(tmp_path) -> None:
    # 重发退避等待期间用户取消：必须立刻 CANCELLED，不能硬等退避结束
    client = FakeResponsesClient([FakeStreamingResponse(_sse(_delta_event("半截")))])
    llm = ResponsesLLM(_settings(), client=client)
    registry = ToolRegistry()
    history = await _new_history(tmp_path)
    loop = AgentLoop(llm, registry, history=history, max_stream_retries=2, retry_backoff_base=30.0)

    try:
        events: list = []
        start = time.perf_counter()
        async for event in loop.run("问"):
            events.append(event)
            if event.type == "turn_retry":
                loop.cancel()
        elapsed = time.perf_counter() - start

        assert events[-1].finish_reason == FinishReason.CANCELLED
        assert elapsed < 5.0  # 没有干等 30 秒的退避
    finally:
        history.close()


# ---------- 切片 7：独立 response.incomplete 是合法的"截断终态"，不是网络故障 ----------
#
# 官方流式事件（千问 Responses）：``response.incomplete`` —— "响应因 max_output_tokens
# 等限制而提前结束"。它是**平台明确告知的终态**：话没说完，但连接没问题。
# 必须走 length（主循环据此静默升档 / 续写），不能像 EOF 那样当网络故障去整轮重发
# ——重发用的是同一个额度，只会再撞一次上限，白烧一轮钱。
# 文档未说明该事件携带 response 体，故缺失时按"事件本义 = 被截断"处理。


def _incomplete_event(reason: str | None = "max_output_tokens", **extra) -> dict:
    """独立截断终态事件；reason=None 模拟平台没给 incomplete_details（千问文档未定义该字段）。"""
    response: dict = {"status": "incomplete"}
    if reason is not None:
        response["incomplete_details"] = {"reason": reason}
    response.update(extra)
    return {"type": "response.incomplete", "response": response}


async def test_independent_incomplete_is_length_not_eof() -> None:
    # 原始探针形态：文本增量之后直接来一个 response.incomplete，流到此结束
    seq = [
        {"type": "response.output_text.delta", "delta": "半截答案"},
        _incomplete_event("max_output_tokens"),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    end = [e for e in events if isinstance(e, TurnEnd)]
    assert len(end) == 1  # 它是合法终态，不是错误
    assert end[0].stop_reason == "length"  # → 主循环升档/续写，而不是网络重试


async def test_independent_incomplete_without_response_body_still_length() -> None:
    # 官方未说明该事件带 response 体：只有 type 时也必须按"被截断"处理，不能被当 EOF
    seq = [
        {"type": "response.output_text.delta", "delta": "半截"},
        {"type": "response.incomplete"},
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    end = [e for e in events if isinstance(e, TurnEnd)]
    assert len(end) == 1
    assert end[0].stop_reason == "length"


async def test_incomplete_reason_other_than_output_limit_is_not_length() -> None:
    # 协议里原因明确不是"撞上输出上限"（如内容过滤）：不该冒充 length 去升档重发
    # ——那不是"没说完"，再生成一次也不会完整。
    seq = [
        {"type": "response.output_text.delta", "delta": "半截"},
        _incomplete_event("content_filter"),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    end = [e for e in events if isinstance(e, TurnEnd)]
    assert len(end) == 1
    assert end[0].stop_reason == "stop"


async def test_independent_incomplete_carries_usage_and_tool_calls_reason() -> None:
    # 截断轮里若已经产出工具调用，结束原因仍以"被截断"优先（工具参数可能被截坏，
    # 交主循环重发拿完整调用，而不是拿残缺参数去执行）
    seq = [
        _function_call_event("call_1", "note", '{"text": "半截'),
        _incomplete_event("max_output_tokens", usage={"input_tokens": 7, "output_tokens": 99}),
    ]

    events = [event async for event in translate_response_events(_dict_events(seq))]

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "length"
    assert end.usage is not None
    assert end.usage.output_tokens == 99


async def test_stream_chat_independent_incomplete_yields_length_turn_end() -> None:
    # 客户端层（原始 SSE 文本）：独立截断事件必须翻译成 length 终态，而不是可重试错误
    response = FakeStreamingResponse(
        _sse(_delta_event("半截"), _incomplete_event("max_output_tokens"))
    )
    client = FakeResponsesClient([response])
    llm = ResponsesLLM(_settings(), client=client)

    events = await _collect_stream(llm)

    end = [e for e in events if isinstance(e, TurnEnd)]
    assert len(end) == 1
    assert end[0].stop_reason == "length"
    assert response.closed


async def test_incomplete_goes_to_max_tokens_upgrade_not_network_retry(tmp_path) -> None:
    # 适配器×主循环：截断必须进入"静默升档"而不是"网络故障整轮重发"，
    # 且半截轮里那个工具调用不能被执行（参数可能已被截断）
    tool = _NoteWriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    client = FakeResponsesClient(
        [
            FakeStreamingResponse(
                _sse(
                    _delta_event("半截"),
                    _function_call_event("call_1", "note"),
                    _incomplete_event("max_output_tokens"),
                )
            ),
            FakeStreamingResponse(_sse(_delta_event("完整答案"), _completed_usage())),
        ]
    )
    llm = ResponsesLLM(_settings(max_tokens=100), client=client)
    history = await _new_history(tmp_path)
    loop = AgentLoop(
        llm,
        registry,
        history=history,
        max_output_tokens=100,
        max_output_tokens_upgrade=200,
        max_stream_retries=2,
        retry_backoff_base=0.0,
    )

    try:
        events = [event async for event in loop.run("写长点")]

        # 重发提示是"放宽上限"，不是"网络中断"
        retries = [e for e in events if e.type == "turn_retry"]
        assert [e.reason for e in retries] == ["max_tokens"]
        # 第二次请求用的是升级后的额度（不是同一个额度再撞一次）
        assert [call["max_output_tokens"] for call in client.calls] == [100, 200]

        # 半截那轮的工具调用没有被执行
        assert tool.calls == []

        result = events[-1]
        assert result.finish_reason == FinishReason.FINAL_TEXT
        assert result.text == "完整答案"
    finally:
        history.close()


# ---------- 切片 8：搜索能力与"摘掉工具"是两件事，必须分别验证 ----------
#
# 三种"工具长什么样"的轮次，语义完全不同，不能因为 payload 里 tools 都是 [] 就混为一谈：
#
# | 轮次 | tools | strip_tools | 该不该声明云端 web_search |
# |---|---|---|---|
# | 纯搜索会话（没挂本地工具） | [] | False | **要**（不然 demo / 搜索用例搜不起来） |
# | 混用轮（有本地 function） | [fn...] | False | 要（与本地工具并排声明） |
# | 续写轮 / 触顶总结轮 | [] | **True** | **不要**（逼模型只动嘴，别去联网、别白烧计费） |
#
# 之前只靠 `bool(tools)` 判断，于是"纯搜索"被误当成"摘工具"，demo 与搜索 integration
# 的请求里根本没有 web_search —— 示例意图与请求语义冲突（T02）。


def test_build_payload_keeps_cloud_search_when_no_local_tools() -> None:
    # 纯搜索会话：一个本地工具都没挂，但云端 web_search 照旧要声明
    llm = ResponsesLLM(_settings(), client=object(), web_search=True)

    payload = llm.build_payload([UserMessage(content="hi")], max_tokens=None, tools=[])

    assert payload["tools"] == [{"type": "web_search"}]


def test_build_payload_mixes_cloud_search_with_local_functions() -> None:
    # 混用轮：云端搜索在前、本地 function 在后，模型自主挑用
    llm = ResponsesLLM(_settings(), client=object(), web_search=True)

    payload = llm.build_payload(
        [UserMessage(content="hi")],
        max_tokens=None,
        tools=[_chat_function_schema("grade_text")],
    )

    assert payload["tools"][0] == {"type": "web_search"}
    assert payload["tools"][1]["name"] == "grade_text"


def test_build_payload_strip_tools_disables_everything() -> None:
    # 摘工具轮（续写 / 触顶总结）：连云端 web_search 一起摘掉
    llm = ResponsesLLM(_settings(), client=object(), web_search=True)

    payload = llm.build_payload(
        [UserMessage(content="hi")], max_tokens=None, tools=[], strip_tools=True
    )

    assert payload["tools"] == []


def test_build_payload_strip_tools_drops_local_functions_too() -> None:
    # 摘工具轮即使手里还有本地 function schema，也不许带出去（否则模型还能调工具）
    llm = ResponsesLLM(_settings(), client=object(), web_search=True)

    payload = llm.build_payload(
        [UserMessage(content="hi")],
        max_tokens=None,
        tools=[_chat_function_schema("grade_text")],
        strip_tools=True,
    )

    assert payload["tools"] == []


async def test_stream_chat_keeps_cloud_search_by_default() -> None:
    # 客户端层：不显式摘工具时，空 tools 也要把云端搜索声明出去
    stream = FakeStreamingResponse(_sse(_delta_event("好"), _completed_usage()))
    client = FakeResponsesClient([stream])
    llm = ResponsesLLM(_settings(), client=client)

    await _collect_stream(llm)

    assert client.calls[0]["tools"] == [{"type": "web_search"}]


async def test_stream_chat_strip_tools_disables_cloud_search() -> None:
    stream = FakeStreamingResponse(_sse(_delta_event("好"), _completed_usage()))
    client = FakeResponsesClient([stream])
    llm = ResponsesLLM(_settings(), client=client)

    async for _ in llm.stream_chat(
        [UserMessage(content="hi")], tools=[], max_tokens=None, strip_tools=True
    ):
        pass

    assert client.calls[0]["tools"] == []


async def test_loop_plain_turn_without_local_tools_still_searches(tmp_path) -> None:
    # 循环级：空注册表的纯搜索会话，请求里必须有 web_search（T02 的核心回归）
    client = FakeResponsesClient(
        [FakeStreamingResponse(_sse(_delta_event("答案"), _completed_usage()))]
    )
    llm = ResponsesLLM(_settings(), client=client)
    history = await _new_history(tmp_path)
    loop = AgentLoop(llm, ToolRegistry(), history=history)

    try:
        events = [event async for event in loop.run("今天六级报名截止了吗")]

        assert events[-1].finish_reason == FinishReason.FINAL_TEXT
        assert client.calls[0]["tools"] == [{"type": "web_search"}]
    finally:
        history.close()


async def test_loop_forced_summary_turn_strips_cloud_search(tmp_path) -> None:
    # 触顶总结轮：摘掉全部工具（本地 + 云端），不许再去联网
    tool = _NoteWriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    client = FakeResponsesClient(
        [
            FakeStreamingResponse(_sse(_function_call_event("call_1", "note"), _completed_usage())),
            FakeStreamingResponse(_sse(_delta_event("总结"), _completed_usage())),
        ]
    )
    llm = ResponsesLLM(_settings(), client=client)
    history = await _new_history(tmp_path)
    loop = AgentLoop(llm, registry, history=history, max_iterations=1)

    try:
        events = [event async for event in loop.run("记一下")]

        # 第一轮：本地工具 + 云端搜索都在
        assert [t["type"] for t in client.calls[0]["tools"]] == ["web_search", "function"]
        # 触顶后的总结轮：全摘
        assert client.calls[1]["tools"] == []
        assert events[-1].finish_reason == FinishReason.MAX_ITERATIONS
    finally:
        history.close()


async def test_loop_continuation_turn_strips_cloud_search(tmp_path) -> None:
    # 续写轮：升档重发仍被截断 → 续写轮必须摘掉全部工具，不许用联网答案替代续写
    client = FakeResponsesClient(
        [
            FakeStreamingResponse(_sse(_delta_event("第一段"), _incomplete_event())),
            FakeStreamingResponse(_sse(_delta_event("第一段"), _incomplete_event())),
            FakeStreamingResponse(_sse(_delta_event("第二段"), _completed_usage())),
        ]
    )
    llm = ResponsesLLM(_settings(), client=client)
    history = await _new_history(tmp_path)
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        max_output_tokens=100,
        max_output_tokens_upgrade=200,
        max_continuations=1,
    )

    try:
        events = [event async for event in loop.run("写长点")]

        # 首次 + 升档重发：都还是普通轮，云端搜索在
        assert client.calls[0]["tools"] == [{"type": "web_search"}]
        assert client.calls[1]["tools"] == [{"type": "web_search"}]
        # 第三次是续写轮：全摘
        assert client.calls[2]["tools"] == []
        assert events[-1].text == "第一段第二段"
    finally:
        history.close()
