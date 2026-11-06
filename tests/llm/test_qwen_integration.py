"""真千问联调冒烟（默认不跑）。

运行方式：.venv\\Scripts\\python -m pytest -m integration
前提：markr/.env 配置了 DASHSCOPE_API_KEY（否则 skipif 跳过）。
"""

import openai
import pytest

from glyph.config import Settings, load_settings
from glyph.llm.base import TextDelta, TurnEnd
from glyph.llm.qwen import QwenLLM
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.tool import Tool
from glyph.session import new_session
from glyph.types import (
    AssistantMessage,
    FinishReason,
    ToolMessage,
    UserMessage,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not load_settings().api_key, reason="未配置 DASHSCOPE_API_KEY"),
]


class EchoTool(Tool):
    name = "echo"
    description = "原样返回输入文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, args: dict) -> dict:
        return {"echoed": args["text"]}


@pytest.fixture
def llm() -> QwenLLM:
    settings: Settings = load_settings()
    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=60.0)
    return QwenLLM(settings, client=client)


async def test_qwen_plain_text_stream(llm: QwenLLM) -> None:
    events = [e async for e in llm.stream_chat([UserMessage(content="用一句话介绍你自己")], [])]

    text = "".join(e.delta for e in events if isinstance(e, TextDelta))
    assert text.strip()  # 真的流式吐了字

    end = [e for e in events if isinstance(e, TurnEnd)][0]
    assert end.stop_reason == "stop"
    assert end.usage is not None
    assert end.usage.output_tokens


async def test_qwen_loop_calls_echo_tool_and_finishes(llm: QwenLLM, tmp_path) -> None:
    settings: Settings = load_settings()
    registry = ToolRegistry()
    registry.register(EchoTool())
    # 用真实 SessionHistory（落在 tmp 目录）：端到端验证 loop 经 HistoryPort 驱动会话落盘
    history = await new_session(
        system_prompt="你是 Glyph 联调助手。",
        model=settings.model,
        cwd=str(tmp_path),
        root=tmp_path,
    )
    loop = AgentLoop(llm, registry, history=history)

    events = [
        e
        async for e in loop.run(
            "请直接调用 echo 工具，把 text 参数设为 hello，然后用一句话说明结果。"
        )
    ]

    results = [e for e in events if e.type == "tool_result"]
    assert results, "模型应当调用 echo 工具（验证边说边跑 + 工具回灌）"
    assert results[0].name == "echo"
    assert results[0].ok is True
    assert results[0].output == {"echoed": "hello"}

    final = events[-1]
    assert final.finish_reason == FinishReason.FINAL_TEXT
    assert final.iterations >= 2

    # 每个 tool_call 都恰好配对一条 tool 消息（无悬空），上下文由 history 重建
    messages = history.messages()
    calls = {call.id for m in messages if isinstance(m, AssistantMessage) for call in m.tool_calls}
    replies = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    assert calls == replies
    history.close()
