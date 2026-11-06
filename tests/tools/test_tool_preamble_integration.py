"""当前主模型在工具调用前说明目的（Responses 通道，默认不联网）。

只测真实主模型的首轮输出，不执行批改模型或访问用户记忆。
调用参数与文字顺序来自实际流；工具 schema 与产品使用的十个工具一致。
显式运行：pytest -o addopts="" -m integration tests/tools/test_tool_preamble_integration.py -s
"""

from __future__ import annotations

import openai
import pytest

from glyph.agent_prompt import build_system_prompt
from glyph.config import load_settings
from glyph.llm.base import TextDelta, ToolCallReady, WebSearchDelta
from glyph.llm.responses import ResponsesLLM
from glyph.tools import DescribeImageTool, GradeTextTool, OcrImageTool, RecheckDetectionTool
from glyph.tools.analyze_writing import AnalyzeWritingTool
from glyph.tools.factory import build_memory
from glyph.tools.file_tools import ReadFileTool
from glyph.tools.lookup_word import LookupWordTool
from glyph.types import SystemMessage, UserMessage

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("user_text", "expected_tool", "purpose_words"),
    [
        ("帮我批改这句话：I go to the library yesterday.", "grade_text", ("检查", "批改")),
        (
            "我今天只有 5 分钟，帮我练一下英语。先读取以前练习记录的正文，再选一个重点。",
            "read_memory",
            ("记忆", "记录", "薄弱", "重点"),
        ),
    ],
    ids=["grade", "memory"],
)
async def test_purpose_text_precedes_real_tool_call(
    tmp_path, user_text, expected_tool, purpose_words
):
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过工具调用说明真模型测试")

    _, memory_tools, injector = build_memory(settings, root=tmp_path / "memory")
    await memory_tools[0].run(
        {"content": "我昨天练过去时，go 写错了；下次先检查过去时。", "title": "过去时练习记录"}
    )
    sources = [
        GradeTextTool,
        OcrImageTool,
        DescribeImageTool,
        RecheckDetectionTool,
        *memory_tools,
        LookupWordTool,
        ReadFileTool,
        AnalyzeWritingTool,
    ]
    schemas = [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in sources
    ]
    messages = [
        SystemMessage(
            content=build_system_prompt(tools=sources, network_capable=settings.web_search)
        ),
        SystemMessage(content=injector.fragment()),
        UserMessage(content=user_text),
    ]
    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
    llm = ResponsesLLM(settings, client=client, web_search=settings.web_search)
    before = ""
    calls = []
    try:
        async for event in llm.stream_chat(messages, schemas, max_tokens=1024):
            if isinstance(event, TextDelta):
                before += event.delta
            elif isinstance(event, ToolCallReady):
                calls.append(event.call.name)
                assert before.strip(), f"{event.call.name} 前没有可见说明"
                assert any(word in before for word in purpose_words), before
            elif isinstance(event, WebSearchDelta):
                assert before.strip(), "联网搜索前没有可见说明"
    finally:
        await client.close()

    print(f"\nmodel={settings.model}; tools={calls}; preamble={before}")
    assert expected_tool in calls, f"未调用目标工具 {expected_tool}: {calls}"
