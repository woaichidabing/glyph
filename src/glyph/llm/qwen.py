"""千问（DashScope OpenAI 兼容模式）流式适配层。

职责边界：把厂商 SSE 的细碎报文（文本碎片、按 index 分片的工具参数 JSON）归并成
llm.base 里规整的语义事件；半截工具参数 JSON 绝不吐出，只在拼全并 json.loads 后才
yield ToolCallReady。loop 因此完全不知道千问 / OpenAI 的报文细节。

错误分层：建连（拿到流之前）的可重试错误做有限次指数退避重试；流式开始后出错不重试
（否则会重复推送已上屏的 delta），统一包成 LLMError(retryable=...) 交给上层。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import openai

from ..config import REASONING_LEVELS, Settings, load_settings
from ..observability import brief
from ..retry import is_context_too_long, is_quota_or_auth_error, is_retryable
from ..types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    Usage,
    UserMessage,
)
from .base import (
    ContextTooLongError,
    LLMError,
    LLMStreamEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallReady,
    TurnEnd,
)

logger = logging.getLogger("glyph.llm.qwen")

# ---------- 内部消息 -> OpenAI 请求形状 ----------


def to_openai_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, UserMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AssistantMessage):
            item: dict = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                    for call in m.tool_calls
                ]
            out.append(item)
        elif isinstance(m, ToolMessage):
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
    return out


# ---------- 厂商 chunk 流 -> 内部语义事件 ----------


async def translate_chunks(chunks) -> AsyncIterator[LLMStreamEvent]:
    """把 openai 流式 chunk 异步迭代器翻译成内部事件（可注入假 chunks 单测）。"""
    # index -> {"id","name","arguments"}，工具参数按分片累积
    acc: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: Usage | None = None

    async for ch in chunks:
        raw_usage = getattr(ch, "usage", None)
        if raw_usage is not None:
            input_tokens = getattr(raw_usage, "prompt_tokens", None) or getattr(
                raw_usage, "input_tokens", None
            )
            output_tokens = getattr(raw_usage, "completion_tokens", None) or getattr(
                raw_usage, "output_tokens", None
            )
            details = getattr(raw_usage, "prompt_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", None) if details else None
            usage = Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens if isinstance(cached_tokens, int) else None,
            )

        choices = getattr(ch, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            content = getattr(delta, "content", None)
            if content:
                yield TextDelta(delta=content)
            # 思考过程（qwen3 系列放在 reasoning_content，与 content 是两条独立的流）：
            # 只展示、不进本轮 blocks、不写账本；空串帧不上屏也不打断流。
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ReasoningDelta(delta=reasoning)
            for piece in getattr(delta, "tool_calls", None) or []:
                slot = acc.setdefault(piece.index, {"id": "", "name": "", "arguments": ""})
                if getattr(piece, "id", None):
                    slot["id"] = piece.id
                fn = getattr(piece, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments

        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

    # 工具参数碎片拼全后一次性解析、吐出；残缺（被截断）的调用绝不空发
    for index in sorted(acc):
        slot = acc[index]
        if not slot["name"]:
            continue  # 连工具名都没拼全，是残片，丢弃（上层会按 length 重发本轮）
        try:
            parsed = json.loads(slot["arguments"] or "{}")
        except json.JSONDecodeError:
            # 参数 JSON 没闭合（典型：输出被 max_tokens 截断）：不发空参调用，
            # 交给上层识别 length 后重发本轮，重新拿完整调用
            continue
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        yield ToolCallReady(
            call=ToolCallBlock(
                id=slot["id"] or f"call_{index}", name=slot["name"], arguments=parsed
            )
        )

    if finish_reason == "tool_calls":
        stop_reason = "tool_calls"
    elif finish_reason == "length":
        stop_reason = "length"
    else:
        stop_reason = "stop"
    yield TurnEnd(stop_reason=stop_reason, usage=usage)


# ---------- 客户端 ----------
# 错误可重试判定不在本文件，统一走 markr/retry.py::is_retryable（全项目单点，R1）


class QwenLLM:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client=None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self._settings = settings or load_settings()
        self.model = self._settings.model
        # 当前模型声明的窗口（0 = 用户没填，压缩层自己找兜底）
        self.model_context_window = int(getattr(self._settings, "model_context_window", 0) or 0)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._client = client or openai.AsyncOpenAI(
            api_key=self._settings.api_key or "missing-api-key",
            base_url=self._settings.base_url,
        )

    async def _open_stream(
        self,
        messages: list[Message],
        tools: list[dict],
        max_tokens: int | None,
        strip_tools: bool = False,
    ):
        payload: dict = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            # 不传则用配置默认值；主循环在输出截断时会显式传升级后的更大额度
            "max_tokens": max_tokens or self._settings.max_tokens,
        }
        if tools and not strip_tools:
            payload["tools"] = tools
        # 思考档：只在**这个模型声明过档位**时才带（自定义档按模型目录里勾的，
        # 内置档由能力表决定）。不带就维持厂商默认，绝不把不认的参数发出去。
        effort = getattr(self._settings, "reasoning_effort", "")
        declared = tuple(getattr(self._settings, "reasoning_levels", ()) or REASONING_LEVELS)
        # "关"不发参数（DSH 的 off 档对应 wire 值 null）——发 "off" 过去厂商会 400
        if (
            getattr(self._settings, "reasoning_supported", False)
            and effort in declared
            and effort != "off"
        ):
            # 参数值优先：模型目录里填了「档位 → 值」就用值，没填就同名（照 DSH 的默认）
            values = getattr(self._settings, "reasoning_values", {}) or {}
            payload["reasoning_effort"] = values.get(effort, effort)

        attempt = 0
        while True:
            try:
                return await self._client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - 统一归类
                # 上下文超长：重发同样消息无意义，直接交主循环走 §10 应急（先减重再重发），
                # 绝不在这里按网络抖动重试。
                if is_context_too_long(exc):
                    raise ContextTooLongError(str(exc)) from exc
                if is_retryable(exc) and attempt < self.max_retries:
                    # 建连重试一行（设计稿 §2.1-C）：第几次 / 退避多久 / 原因摘要。
                    # 两条通道（qwen / responses）对齐打，避免走哪条通道可观测性不一样。
                    logger.info(
                        "建连失败重试 attempt=%d 退避=%.1fs 原因=%s",
                        attempt + 1,
                        self.backoff_base * (2**attempt),
                        brief(exc),
                    )
                    await asyncio.sleep(self.backoff_base * (2**attempt))
                    attempt += 1
                    continue
                raise LLMError(
                    str(exc),
                    retryable=is_retryable(exc),
                    quota_or_auth=is_quota_or_auth_error(exc),
                ) from exc

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict],
        max_tokens: int | None = None,
        strip_tools: bool = False,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Chat 通道没有平台内置工具，摘工具 == 不带 tools（与旧行为一致）。

        参数 ``strip_tools`` 只为满足统一协议而存在：这里"空 tools 就不传 tools 键"
        本来就等于把全部工具摘掉，故只需在判空时一并考虑它。

        参数 ``model`` 同样仅为协议一致而接收：主脑额度兜底只走 Responses 通道
        （生产主脑是 ResponsesLLM），Chat 通道不做运行时换脑，故在此显式忽略。
        """
        del model  # Chat 通道不支持运行时换脑，见上
        stream = await self._open_stream(messages, tools, max_tokens, strip_tools)
        try:
            async for event in translate_chunks(stream):
                yield event
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - 流式开始后失败不重试，直接归类上抛
            # 流中途收到上下文超长：归类为 ContextTooLongError，交主循环 §10 先减重再重发
            if is_context_too_long(exc):
                raise ContextTooLongError(str(exc)) from exc
            raise LLMError(
                str(exc),
                retryable=is_retryable(exc),
                quota_or_auth=is_quota_or_auth_error(exc),
            ) from exc
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                await close()
