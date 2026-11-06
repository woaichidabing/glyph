"""真千问 Responses + 平台内置 web_search 联调冒烟（默认不跑）。

运行方式：
    .venv\\Scripts\\python -m pytest -o addopts="" -m integration \
        tests/llm/test_responses_integration.py
前提：markr/.env 配置了 GLYPH_API_KEY / DASHSCOPE_API_KEY（否则 skipif 跳过）。

这些用例真的会联网、真的按次计费（每次约几厘钱），只在显式 -m integration 时运行。
"""

import openai
import pytest

from glyph.config import Settings, load_settings
from glyph.llm.base import TextDelta, TurnEnd, WebSearchDelta
from glyph.llm.responses import ResponsesLLM
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.session import new_session
from glyph.types import FinishReason, UserMessage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not load_settings().api_key, reason="未配置 API Key"),
]

# 需要最新事实、模型不联网就答不准的问题，逼出真实的 web_search 调用
QUESTION = "请联网搜索后用中文一句话回答：2026 年下半年全国大学英语六级笔试是哪天？"


@pytest.fixture
def llm() -> ResponsesLLM:
    settings: Settings = load_settings()
    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=120.0)
    return ResponsesLLM(settings, client=client, web_search=True)


async def test_responses_web_search_streams_sources_and_count(llm: ResponsesLLM) -> None:
    # tools=[] 的语义是"这一轮没有本地工具"，**不是**"禁用全部工具"——
    # 平台内置搜索照旧开着（T02）。要连搜索一起摘掉必须显式 strip_tools=True。
    events = [e async for e in llm.stream_chat([UserMessage(content=QUESTION)], [])]

    searches = [e for e in events if isinstance(e, WebSearchDelta)]
    assert searches, "应出现平台 web_search 进度事件"
    completed = [s for s in searches if s.phase == "completed"]
    assert completed, "应出现 completed 搜索事件"
    assert completed[-1].sources, "completed 事件应带回至少一个来源 URL"
    assert all(source.url for source in completed[-1].sources)

    text = "".join(e.delta for e in events if isinstance(e, TextDelta))
    assert text.strip()  # 搜索后真的产出了正文答案

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "stop"
    assert end.web_search_count is not None
    assert end.web_search_count >= 1  # 计费计数被透传


async def test_responses_strip_tools_disables_cloud_search(llm: ResponsesLLM) -> None:
    # 摘工具轮（续写 / 触顶总结）连平台搜索一起关：请求里 tools 必须是空数组，
    # 模型只能动嘴，一个 web_search 事件都不该出现，计数也必须是 0（T02）。
    events = [
        e async for e in llm.stream_chat([UserMessage(content=QUESTION)], [], strip_tools=True)
    ]

    assert not [e for e in events if isinstance(e, WebSearchDelta)]

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.web_search_count in (None, 0)


async def test_responses_loop_web_search_end_to_end(llm: ResponsesLLM, tmp_path) -> None:
    settings: Settings = load_settings()
    # 空注册表：阶段 1 搜索全在云端，本地一个工具都不挂
    history = await new_session(
        system_prompt="你是 Glyph 英语学习助手。",
        model=settings.model,
        cwd=str(tmp_path),
        root=tmp_path,
    )
    loop = AgentLoop(llm, ToolRegistry(), history=history)

    try:
        events = [e async for e in loop.run(QUESTION)]

        assert any(e.type == "web_search" for e in events)

        result = events[-1]
        assert result.finish_reason == FinishReason.FINAL_TEXT
        assert result.text.strip()
        assert result.web_search_count >= 1

        # 搜索过程不落账本：只有 system / user / 一条完整 assistant
        assert [m.role for m in history.messages()] == ["system", "user", "assistant"]
    finally:
        history.close()
