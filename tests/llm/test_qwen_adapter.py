"""千问适配层的不联网单测：内部消息 ↔ OpenAI 形状映射、流式碎片累积。

真 HTTP 冒烟在 test_qwen_integration.py（marker=integration，默认跳过）。
"""

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from glyph.llm.base import LLMError, ReasoningDelta, TextDelta, ToolCallReady, TurnEnd
from glyph.llm.qwen import QwenLLM, to_openai_messages, translate_chunks
from glyph.retry import is_quota_or_auth_error
from glyph.types import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolMessage,
    UserMessage,
)

# ---------- 消息映射 ----------


def test_to_openai_messages_covers_all_roles() -> None:
    messages = [
        SystemMessage(content="sys"),
        UserMessage(content="hi"),
        AssistantMessage(
            blocks=[
                TextBlock(text="我调用"),
                ToolCallBlock(id="c1", name="echo", arguments={"text": "hi"}),
            ]
        ),
        ToolMessage(tool_call_id="c1", name="echo", content='{"echoed": "hi"}'),
        AssistantMessage(blocks=[TextBlock(text="结果")]),
    ]

    out = to_openai_messages(messages)

    assert out[0] == {"role": "system", "content": "sys"}
    assert out[1] == {"role": "user", "content": "hi"}

    assert out[2]["role"] == "assistant"
    assert out[2]["content"] == "我调用"
    (tool_call,) = out[2]["tool_calls"]
    assert tool_call["id"] == "c1"
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "echo"
    assert json.loads(tool_call["function"]["arguments"]) == {"text": "hi"}

    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"echoed": "hi"}'}

    assert out[4] == {"role": "assistant", "content": "结果"}
    assert "tool_calls" not in out[4]


def test_assistant_with_only_tool_calls_has_null_text_content() -> None:
    messages = [
        AssistantMessage(blocks=[ToolCallBlock(id="c9", name="t", arguments={})]),
    ]
    out = to_openai_messages(messages)
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["id"] == "c9"


# ---------- 流式碎片累积 ----------


def _chunk(content=None, tool_calls=None, finish=None, reasoning_content=None):
    return NS(
        choices=[
            NS(
                delta=NS(
                    content=content, tool_calls=tool_calls, reasoning_content=reasoning_content
                ),
                finish_reason=finish,
            )
        ]
    )


def _usage_chunk(prompt_tokens, completion_tokens, cached_tokens=None):
    details = None if cached_tokens is None else NS(cached_tokens=cached_tokens)
    return NS(
        choices=[],
        usage=NS(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=details,
        ),
    )


def _tc(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


async def _aiter(items):
    for item in items:
        await asyncio.sleep(0)
        yield item


async def test_accumulates_text_deltas_and_fragmented_tool_arguments() -> None:
    chunks = _aiter(
        [
            _chunk(content="你"),
            _chunk(content="好"),
            _chunk(tool_calls=[_tc(0, id="c1", name="echo", arguments='{"text":')]),
            _chunk(tool_calls=[_tc(0, arguments=' "hi"}')]),
            _chunk(finish="tool_calls"),
            _usage_chunk(7, 3),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]

    deltas = [e for e in events if isinstance(e, TextDelta)]
    assert "".join(e.delta for e in deltas) == "你好"

    ready = [e for e in events if isinstance(e, ToolCallReady)]
    assert len(ready) == 1
    assert ready[0].call.id == "c1"
    assert ready[0].call.name == "echo"
    assert ready[0].call.arguments == {"text": "hi"}

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "tool_calls"
    assert end.usage.input_tokens == 7
    assert end.usage.output_tokens == 3


async def test_plain_text_stream_ends_with_stop_and_no_tool_calls() -> None:
    chunks = _aiter(
        [
            _chunk(content="A"),
            _chunk(content="B"),
            _chunk(finish="stop"),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]

    assert any(isinstance(e, TextDelta) for e in events)
    assert not any(isinstance(e, ToolCallReady) for e in events)
    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "stop"


async def test_length_finish_reason_is_surfaced_not_mistaken_for_stop() -> None:
    # 输出撞上 max_tokens 上限（话说一半）：finish_reason=length 必须如实上报
    chunks = _aiter(
        [
            _chunk(content="说了一半"),
            _chunk(finish="length"),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "length"


async def test_truncated_tool_arguments_are_not_emitted_as_empty_call() -> None:
    # 工具参数 JSON 还没闭合就被 length 截断：绝不能吐出一个空参数的工具调用
    chunks = _aiter(
        [
            _chunk(tool_calls=[_tc(0, id="c1", name="echo", arguments='{"text":')]),
            _chunk(finish="length"),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]

    assert [e for e in events if isinstance(e, ToolCallReady)] == []
    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "length"


# ---------- 包装原始异常时不能丢掉"这个模型用不了"的结论 ----------


class _InvalidKeyError(Exception):
    """千问兼容端点钥匙不对时抛的原样异常（带状态码 + 平台原文）。"""

    status_code = 401

    def __init__(self) -> None:
        super().__init__(
            "Error code: 401 - {'code': 'InvalidApiKey', "
            "'message': 'Invalid API-key provided.', 'request_id': '20c92e0f'}"
        )


class _ExplodingCompletions:
    async def create(self, **kwargs):
        raise _InvalidKeyError()


class _ExplodingClient:
    def __init__(self) -> None:
        self.chat = NS(completions=_ExplodingCompletions())


async def test_usage_carries_cached_tokens() -> None:
    """用量里带出"命中缓存的 token 数"——界面靠它算这一轮省了多少钱。

    取真机量级：一轮问答输入 1.5 万上下，热的时候 9 成以上命中。
    """
    chunks = _aiter(
        [
            _chunk(content="好"),
            _chunk(finish="stop"),
            _usage_chunk(15074, 267, cached_tokens=14336),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]
    end = [e for e in events if isinstance(e, TurnEnd)][0]

    assert end.usage.input_tokens == 15074
    assert end.usage.cached_tokens == 14336


async def test_usage_without_cached_field_stays_unknown() -> None:
    """厂商没给这个字段（老网关 / 假模型）时保持 None —— 界面显示"未知"，不谎报 0。"""
    chunks = _aiter([_chunk(content="好"), _chunk(finish="stop"), _usage_chunk(100, 5)])

    events = [event async for event in translate_chunks(chunks)]
    end = [e for e in events if isinstance(e, TurnEnd)][0]

    assert end.usage.cached_tokens is None


async def test_auth_failure_keeps_quota_or_auth_flag_after_wrapping() -> None:
    """适配层统一把异常包成 LLMError，状态码在这一层就没了。

    包装时必须把"是不是额度 / 鉴权错"的结论一起带上：否则主循环只剩一段英文文案，
    既不会自动换脑，还会把平台原文直接甩给用户（2026-10-12 真机 401）。
    """
    llm = QwenLLM(client=_ExplodingClient(), max_retries=0)

    with pytest.raises(LLMError) as exc:
        [event async for event in llm.stream_chat([UserMessage(content="hi")], tools=[])]

    assert is_quota_or_auth_error(exc.value) is True


# ---------- 思考过程（展示型，不进账本） ----------


async def test_reasoning_content_is_surfaced_as_reasoning_delta() -> None:
    """qwen3 系列把思考放在 delta.reasoning_content，与 delta.content 是两条流。

    2026-10-12 用户真机确认主脑会吐思考文本，而适配层此前只读 content，
    思考被静默丢弃。这次必须接住并如实上报——但**只展示**：不进本轮 blocks、
    不写账本（见 events.py 的展示型事件三分法）。
    """
    chunks = _aiter(
        [
            _chunk(reasoning_content="用户问的是"),
            _chunk(reasoning_content="语法错误。"),
            _chunk(content="好"),
            _chunk(finish="stop"),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]

    think = [e for e in events if isinstance(e, ReasoningDelta)]
    assert "".join(e.delta for e in think) == "用户问的是语法错误。"
    # 思考与正文绝不相混：正文里不能混进思考，反之亦然
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "好"


async def test_chunk_without_reasoning_field_emits_no_reasoning_delta() -> None:
    """不吐思考的模型 / 轮次：一条 ReasoningDelta 都不许发（空思考不占位）。"""
    chunks = _aiter([_chunk(content="好"), _chunk(finish="stop")])

    events = [event async for event in translate_chunks(chunks)]

    assert [e for e in events if isinstance(e, ReasoningDelta)] == []


async def test_empty_reasoning_string_is_not_emitted() -> None:
    """空串思考帧（有些网关会发空 delta）不许上屏，也不许打断流。"""
    chunks = _aiter(
        [
            _chunk(reasoning_content=""),
            _chunk(content="好"),
            _chunk(finish="stop"),
        ]
    )

    events = [event async for event in translate_chunks(chunks)]

    assert [e for e in events if isinstance(e, ReasoningDelta)] == []
    assert "".join(e.delta for e in events if isinstance(e, TextDelta)) == "好"


# ---------- 思考档：按"模型声明的档位"发，极高 / 最高也要发出去 ----------


class _CapturingCompletions:
    """记下请求参数，然后给一段最小流（只为让 stream_chat 正常收尾）。"""

    def __init__(self) -> None:
        self.seen: dict = {}

    async def create(self, **kwargs):
        self.seen = kwargs
        return _aiter([_chunk(content="好"), _chunk(finish="stop")])


class _CapturingClient:
    def __init__(self) -> None:
        self.completions = _CapturingCompletions()
        self.chat = NS(completions=self.completions)


async def _payload_for(effort: str, levels: tuple[str, ...]) -> dict:
    from dataclasses import replace as dc_replace

    from glyph.config import load_settings
    from glyph.llm.qwen import QwenLLM
    from glyph.types import UserMessage

    client = _CapturingClient()
    settings = dc_replace(
        load_settings(),
        model="my-model",
        api_key="k",
        reasoning_effort=effort,
        reasoning_supported=True,
        reasoning_levels=levels,
        reasoning_values={},
    )
    llm = QwenLLM(settings, client=client)
    async for _ in llm.stream_chat([UserMessage(content="hi")], tools=None, max_tokens=8):
        pass
    return client.completions.seen


async def test_high_levels_are_sent_when_declared() -> None:
    """极高 / 最高也是合法档位：模型声明了就要发出去（2026-10-28 真机踩到"上不去"）。"""
    for effort in ("high", "xhigh", "max"):
        payload = await _payload_for(effort, ("low", "medium", "high", "xhigh", "max"))
        assert payload.get("reasoning_effort") == effort


async def test_level_not_declared_is_not_sent() -> None:
    """模型没声明这一档就不发，避免把厂商不认的参数发出去招 400。"""
    payload = await _payload_for("max", ("low", "medium", "high"))
    assert "reasoning_effort" not in payload

async def test_off_level_sends_nothing() -> None:
    """选「关」时不能把 "off" 发出去（DSH 的 off 档 wire 值是 null）。"""
    payload = await _payload_for("off", ("off", "low", "medium", "high"))
    assert "reasoning_effort" not in payload