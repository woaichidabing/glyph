"""FallbackChatClient 离线测试：首选子模型没额度 / 没权限时自动换同端点模型。

只钉死"换不换、换的顺序、什么错不该换"，不发任何真实请求（用假 client）。
async 用 asyncio.run 自包，不依赖 pytest-asyncio 是否配置。
"""

import asyncio

import pytest

from glyph.llm.fallback import FallbackChatClient


class _Err(Exception):
    """最朴素的假 HTTP/平台错误：可带 status_code，也可只带一段错误文案。"""

    def __init__(self, status=None, text="boom"):
        super().__init__(text)
        if status is not None:
            self.status_code = status


class _Completions:
    def __init__(self, behavior):
        self._behavior = behavior
        self.calls: list[str] = []

    async def create(self, **kwargs):
        model = kwargs.get("model")
        self.calls.append(model)
        outcome = self._behavior(model)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Chat:
    def __init__(self, comp):
        self.completions = comp


class _Client:
    """最小可用的 OpenAI 兼容假客户端：chat.completions.create + 一个无关属性。"""

    def __init__(self, comp):
        self.chat = _Chat(comp)
        self.touched = "other-attr"


def _run(coro):
    return asyncio.run(coro)


def test_switches_to_fallback_on_403():
    """首选 403 配额耗尽 → 自动换降级模型，且调用顺序是 [首选, 降级]。"""
    comp = _Completions(
        lambda m: _Err(403, "free quota exhausted") if m == "glm-5.3" else {"ok": m}
    )
    client = FallbackChatClient(_Client(comp), primary="glm-5.3", fallbacks=["deepseek-v4.1-flash"])
    result = _run(client.chat.completions.create(messages=[]))
    assert result == {"ok": "deepseek-v4.1-flash"}
    assert comp.calls == ["glm-5.3", "deepseek-v4.1-flash"]


def test_primary_success_does_not_switch():
    """首选好好的，绝不多此一举去调降级模型。"""
    comp = _Completions(lambda m: {"ok": m})
    client = FallbackChatClient(_Client(comp), primary="glm-5.3", fallbacks=["deepseek-v4.1-flash"])
    result = _run(client.chat.completions.create(model="glm-5.3"))
    assert result == {"ok": "glm-5.3"}
    assert comp.calls == ["glm-5.3"]


def test_server_error_does_not_switch():
    """5xx 是临时故障，交给调用方对同一模型重试；这里不许擅自换模型。"""
    comp = _Completions(lambda m: _Err(500, "internal server error"))
    client = FallbackChatClient(_Client(comp), primary="glm-5.3", fallbacks=["deepseek-v4.1-flash"])
    with pytest.raises(_Err):
        _run(client.chat.completions.create())
    assert comp.calls == ["glm-5.3"]


def test_rate_limit_429_does_not_switch():
    """429 是临时挤爆，同模型等会儿再试即可，不算"模型用不了"。"""
    comp = _Completions(lambda m: _Err(429, "rate limit"))
    client = FallbackChatClient(_Client(comp), primary="glm-5.3", fallbacks=["deepseek-v4.1-flash"])
    with pytest.raises(_Err):
        _run(client.chat.completions.create())
    assert comp.calls == ["glm-5.3"]


def test_all_candidates_quota_exhausted_raises_last_error():
    """首选和降级都没额度：把最后一个异常原样抛回，不能假装成功。"""
    comp = _Completions(lambda m: _Err(403, f"{m} forbidden"))
    client = FallbackChatClient(_Client(comp), primary="glm-5.3", fallbacks=["deepseek-v4.1-flash"])
    with pytest.raises(_Err):
        _run(client.chat.completions.create())
    assert comp.calls == ["glm-5.3", "deepseek-v4.1-flash"]


def test_quota_hint_without_status_code_still_switches():
    """平台只给错误文案（无 status_code）也能认出"额度耗尽"并换模型。"""
    comp = _Completions(
        lambda m: (
            _Err(text="Free quota exhausted, please add funds") if m == "glm-5.3" else {"ok": m}
        )
    )
    client = FallbackChatClient(_Client(comp), primary="glm-5.3", fallbacks=["deepseek-v4.1-flash"])
    result = _run(client.chat.completions.create())
    assert result == {"ok": "deepseek-v4.1-flash"}
    assert comp.calls == ["glm-5.3", "deepseek-v4.1-flash"]


def test_primary_equal_to_a_fallback_is_deduped():
    """首选恰好也出现在降级列表里时只调一次，去重保序。"""
    comp = _Completions(lambda m: _Err(403, "forbidden") if m == "same" else {"ok": m})
    client = FallbackChatClient(
        _Client(comp), primary="same", fallbacks=["same", "deepseek-v4.1-flash"]
    )
    result = _run(client.chat.completions.create())
    assert result == {"ok": "deepseek-v4.1-flash"}
    assert comp.calls == ["same", "deepseek-v4.1-flash"]


def test_other_client_attrs_pass_through():
    """除 chat.completions.create 外，被包装 client 的其它属性照常可用。"""
    client = FallbackChatClient(_Client(_Completions(lambda m: {})), primary="a", fallbacks=[])
    assert client.touched == "other-attr"
