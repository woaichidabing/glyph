"""上下文超长归类（§10）：retry 单点判定 + qwen 适配层把它抛成 ContextTooLongError。

关键区别：上下文超长**不是**可无限重试的网络抖动——重发同样消息必然再撞一次，
必须先减重再重发。故 5xx 哪怕文案撞 "too long" 也不得误判成长。
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from glyph.llm.base import ContextTooLongError, LLMError
from glyph.llm.qwen import QwenLLM
from glyph.retry import is_context_too_long


class _HTTPExc(Exception):
    def __init__(self, status: int | None, msg: str) -> None:
        self.status_code = status
        super().__init__(msg)


class _RaisingCompletions:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def create(self, **kwargs):
        raise self._exc


class _RaisingChat:
    def __init__(self, exc: Exception) -> None:
        self.completions = _RaisingCompletions(exc)


class _RaisingClient:
    def __init__(self, exc: Exception) -> None:
        self.chat = _RaisingChat(exc)


class _BadStream:
    """建连成功、但迭代流时抛上下文超长（模拟流中途才收到 400 context_length）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc

    async def close(self) -> None:
        pass


class _BadStreamCompletions:
    def __init__(self, stream: _BadStream) -> None:
        self._stream = stream

    async def create(self, **kwargs):
        return self._stream


class _BadStreamChat:
    def __init__(self, stream: _BadStream) -> None:
        self.completions = _BadStreamCompletions(stream)


class _BadStreamClient:
    def __init__(self, stream: _BadStream) -> None:
        self.chat = _BadStreamChat(stream)


def _llm(client) -> QwenLLM:
    settings = NS(
        model="glm-5.2",
        base_url="https://example.test/v1",
        api_key="k",
        max_tokens=100,
    )
    return QwenLLM(settings, client=client, max_retries=0, backoff_base=0.0)


# ---------- retry 单点判定 ----------


@pytest.mark.parametrize(
    "exc",
    [
        _HTTPExc(413, "request entity too large"),
        _HTTPExc(400, "This model's maximum context length is 8192 tokens"),
        _HTTPExc(400, "context_length_exceeded: please reduce the length"),
        _HTTPExc(None, "context length exceeded"),
        ContextTooLongError("already classified"),
    ],
)
def test_is_context_too_long_recognizes(exc: Exception) -> None:
    assert is_context_too_long(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _HTTPExc(500, "internal: request took too long to process"),  # 5xx 撞词也不算
        _HTTPExc(502, "bad gateway"),
        _HTTPExc(400, "invalid request parameters"),  # 400 但无超长文案
        _HTTPExc(429, "rate limit"),
        _HTTPExc(None, "connection reset by peer"),
    ],
)
def test_is_context_too_long_rejects(exc: Exception) -> None:
    assert is_context_too_long(exc) is False


# ---------- qwen 适配层归类 ----------


async def test_qwen_open_stream_413_raises_context_too_long() -> None:
    llm = _llm(_RaisingClient(_HTTPExc(413, "413 Request Entity Too Large")))
    with pytest.raises(ContextTooLongError):
        await llm._open_stream([], [], None)


async def test_qwen_open_stream_400_context_hint_raises_context_too_long() -> None:
    llm = _llm(_RaisingClient(_HTTPExc(400, "context_length_exceeded: input too long")))
    with pytest.raises(ContextTooLongError):
        await llm._open_stream([], [], None)


async def test_qwen_open_stream_500_stays_retryable_llm_error() -> None:
    llm = _llm(_RaisingClient(_HTTPExc(500, "internal server error")))
    with pytest.raises(LLMError) as exc_info:
        await llm._open_stream([], [], None)
    assert not isinstance(exc_info.value, ContextTooLongError)
    assert exc_info.value.retryable is True


async def test_qwen_stream_midflight_context_error_classified() -> None:
    stream = _BadStream(_HTTPExc(400, "maximum context length exceeded"))
    llm = _llm(_BadStreamClient(stream))
    with pytest.raises(ContextTooLongError):
        async for _ in llm.stream_chat([], []):
            pass
