"""主脑额度兜底：零产出撞额度 / 鉴权错误时，自动临时换 fallback 脑（默认 kimi-k3）重发本轮。

与子模型 ``FallbackChatClient`` 的"撞 403 就换模型"同一思路，但主脑是流式的，多一条
红线：**只有本轮一个字没吐、没声明工具、没联网**（换脑重发绝对幂等、不会重复上屏 /
重复跑工具 / 重复花联网的钱）才自动换。一旦已经产出过任何东西，就维持原额度报错、
指路手动换，不偷偷重来。

其余边界：
- 每个 fallback 只试一次，全部用不了就回到原额度报错，不无限循环；
- 只在本次 run 内临时换脑（后续轮沿用兜底脑），**不写回用户持久选择的模型**。
"""

from __future__ import annotations

from fakes import InMemoryHistory

from glyph.events import (
    ErrorEvent,
    ResultEvent,
    TurnRetryEvent,
)
from glyph.events import (
    TextDelta as TextDeltaEvent,
)
from glyph.llm.base import LLMError, TextDelta, ToolCallReady, TurnEnd, WebSearchDelta
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.types import ToolCallBlock

# 真实平台样本里的额度耗尽文案（is_quota_or_auth_error 靠关键词认，见 test_retry）
QUOTA_TEXT = "Free quota exhausted. To continue, please add funds."


class _ScriptedFallbackLLM:
    """按调用次序脚本化：每一步要么在首个事件前抛额度错，要么吐一段文本；记录每次的 model。"""

    def __init__(self, script, *, primary: str = "deepseek-v4.1-flash") -> None:
        self._script = list(script)
        # loop 用 getattr(llm, "model") 取首选脑名字，故替身也要暴露 model
        self.model = primary
        self.models: list[str | None] = []
        self.calls = 0

    async def stream_chat(self, messages, tools, max_tokens=None, strip_tools=False, model=None):
        self.models.append(model)
        self.calls += 1
        step = self._script.pop(0)
        if step.get("raise_error") is not None:
            # 适配层包装后的真实形态：状态码已经丢了，只剩一段文案（可带算好的归类标记）
            raise step["raise_error"]
        if step.get("quota"):
            # 建连阶段（还没吐出任何事件）就撞额度，等价于 ResponsesLLM._open_stream 抛错
            raise LLMError(QUOTA_TEXT, retryable=False)
        yield TextDelta(delta=step.get("text", "好的"))
        yield TurnEnd(stop_reason="stop", usage=None)


def _run(llm, fallbacks=("kimi-k3",)) -> AgentLoop:
    return AgentLoop(
        llm,
        ToolRegistry(),
        history=InMemoryHistory(),
        main_fallback_models=fallbacks,
        retry_backoff_base=0.0,
    )


async def _collect(loop: AgentLoop):
    return [event async for event in loop.run("你好")]


async def test_quota_before_any_output_switches_to_fallback_and_succeeds() -> None:
    llm = _ScriptedFallbackLLM([{"quota": True}, {"text": "兜底回答"}])
    events = await _collect(_run(llm))

    # 第一次用首选脑（model=None）撞额度，第二次带 model=kimi-k3 重发
    assert llm.models == [None, "kimi-k3"]

    retries = [e for e in events if isinstance(e, TurnRetryEvent)]
    assert len(retries) == 1
    assert retries[0].reason == "model_fallback"
    # 告知文案要让用户看懂：谁没额度、换成了谁
    assert "kimi-k3" in retries[0].message
    assert "deepseek-v4.1-flash" in retries[0].message

    # 兜底成功：有终态结果、兜底文本逐字上屏、没有错误事件
    assert any(isinstance(e, ResultEvent) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert "".join(e.delta for e in events if isinstance(e, TextDeltaEvent)) == "兜底回答"


async def test_quota_after_text_started_does_not_switch() -> None:
    """已吐了半个字才撞额度：换脑会重复上屏 / 重复花钱，必须直接报错、不自动换。"""

    class _HalfThenQuota:
        model = "deepseek-v4.1-flash"

        def __init__(self) -> None:
            self.models: list[str | None] = []
            self.calls = 0

        async def stream_chat(
            self, messages, tools, max_tokens=None, strip_tools=False, model=None
        ):
            self.models.append(model)
            self.calls += 1
            yield TextDelta(delta="半截")
            raise LLMError(QUOTA_TEXT, retryable=False)

    llm = _HalfThenQuota()
    events = await _collect(_run(llm))

    assert llm.calls == 1  # 只调了一次，没有换脑重发
    assert not any(isinstance(e, TurnRetryEvent) and e.reason == "model_fallback" for e in events)
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_quota_after_tool_declared_does_not_switch() -> None:
    """模型已声明要调工具（ToolCallReady 已进本轮 blocks）才撞额度：不换脑。"""

    class _ToolThenQuota:
        model = "deepseek-v4.1-flash"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(
            self, messages, tools, max_tokens=None, strip_tools=False, model=None
        ):
            self.calls += 1
            yield ToolCallReady(call=ToolCallBlock(id="c1", name="lookup_word", arguments={}))
            raise LLMError(QUOTA_TEXT, retryable=False)

    llm = _ToolThenQuota()
    events = await _collect(_run(llm))

    assert llm.calls == 1
    assert not any(isinstance(e, TurnRetryEvent) and e.reason == "model_fallback" for e in events)
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_quota_after_web_search_does_not_switch() -> None:
    """平台联网已经在云端发生（按次计费）才撞额度：换脑重发会再搜一次、重复花钱，不换。"""

    class _SearchThenQuota:
        model = "deepseek-v4.1-flash"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(
            self, messages, tools, max_tokens=None, strip_tools=False, model=None
        ):
            self.calls += 1
            yield WebSearchDelta(phase="started", search_id="s1", query="CET6", sources=[])
            raise LLMError(QUOTA_TEXT, retryable=False)

    llm = _SearchThenQuota()
    events = await _collect(_run(llm))

    assert llm.calls == 1
    assert not any(isinstance(e, TurnRetryEvent) and e.reason == "model_fallback" for e in events)
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_fallback_also_quota_gives_up_without_looping() -> None:
    """首选脑和兜底脑都没额度：各试一次后回到额度报错，不无限循环。"""
    llm = _ScriptedFallbackLLM([{"quota": True}, {"quota": True}])
    events = await _collect(_run(llm, fallbacks=("kimi-k3",)))

    assert llm.models == [None, "kimi-k3"]
    model_retries = [
        e for e in events if isinstance(e, TurnRetryEvent) and e.reason == "model_fallback"
    ]
    assert len(model_retries) == 1  # 只尝试换一次
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_no_fallback_configured_goes_straight_to_error() -> None:
    """没配兜底脑：撞额度直接报错，不重发。"""
    llm = _ScriptedFallbackLLM([{"quota": True}])
    events = await _collect(_run(llm, fallbacks=()))

    assert llm.calls == 1
    assert not any(isinstance(e, TurnRetryEvent) and e.reason == "model_fallback" for e in events)
    assert any(isinstance(e, ErrorEvent) for e in events)


# 2026-10-12 真机样本：千问兼容端点钥匙不对时，平台回的原文（HTTP 401）
REAL_401_TEXT = (
    "Error code: 401 - {'code': 'InvalidApiKey', 'message': 'Invalid API-key provided.', "
    "'request_id': '20c92e0f-65ca-9c79-8bea-3cd793b5faa0'}"
)


async def test_invalid_key_text_from_platform_still_switches_brain() -> None:
    """适配层包过一层之后只剩这段英文（状态码丢了）：也必须认得这是"这个模型用不了"，
    照常换兜底脑重发。认不出来就会既换不了脑、又把英文原文当成回答甩给用户。"""
    llm = _ScriptedFallbackLLM(
        [{"raise_error": LLMError(REAL_401_TEXT, retryable=False)}, {"text": "兜底回答"}]
    )
    events = await _collect(_run(llm))

    assert llm.models == [None, "kimi-k3"]
    assert any(isinstance(e, ResultEvent) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)


async def test_wrapped_auth_error_flag_still_switches_brain() -> None:
    """适配层包装时算好的归类标记同样认（文案可能完全看不出来是什么错）。"""
    llm = _ScriptedFallbackLLM(
        [
            {"raise_error": LLMError("模型调用失败", retryable=False, quota_or_auth=True)},
            {"text": "兜底回答"},
        ]
    )
    events = await _collect(_run(llm))

    assert llm.models == [None, "kimi-k3"]
    assert any(isinstance(e, ResultEvent) for e in events)
