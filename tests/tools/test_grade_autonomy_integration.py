"""主模型自主编排集成测试（真模型，默认跳过，用 -m integration 运行）。

金标集成测试只证明 grade_text 内部批改质量；本测试证明另一件事：**真实主对话
模型在看到"帮我批改"这类请求时，会自己决定调用 grade_text 工具**，并在拿到结构化
结果后走完"回灌 → 短引导 → 正常收尾"。这是"agent 有没有编排能力"的直接证据。

为省 token、隔离批改质量的波动，grade_text 内部的批改器注入固定假结果（离线、
确定性），本地拼写器仍用真实的；被测对象是真实主模型的 function-calling 决策。

运行（在 markr/ 目录）：
    .venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration \\
        tests/tools/test_grade_autonomy_integration.py -s
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from glyph.agent_prompt import SYSTEM_PROMPT
from glyph.config import load_settings
from glyph.llm.qwen import QwenLLM
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.tools.grade import GradeTextTool
from glyph.tools.grading_models import Detection, GradeResult
from glyph.tools.spell import PySpellingChecker

# 复用 runtime 层的内存历史（各测试目录无 __init__，手动加入路径）
_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(_RUNTIME))
from fakes import InMemoryHistory  # noqa: E402


class _FixedGrader:
    """固定批改结果：不联网、不花钱，让本测试只聚焦主模型是否自主调用工具。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def grade(self, text: str) -> GradeResult:
        self.calls.append(text)
        return GradeResult(
            detections=[
                Detection(
                    original="I is",
                    corrected="I am",
                    type="hard",
                    tier="error",
                    explanation_zh="主谓不一致，主语 I 要搭配 am",
                )
            ],
            summary="基础不错，注意 be 动词",
        )


@pytest.mark.integration
async def test_main_model_autonomously_calls_grade_text(tmp_path):
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过真模型自主编排测试")

    essay = "I is a studdent."
    grader = _FixedGrader()
    registry = ToolRegistry()
    registry.register(GradeTextTool(grader=grader, spell=PySpellingChecker()))

    # SYSTEM_PROMPT 默认含记忆三件套：注册真实工具（临时目录），保证清单一致
    from glyph.tools.factory import build_memory

    _store, mem_tools, _injector = build_memory(settings, root=tmp_path / "memory")
    for mem_tool in mem_tools:
        registry.register(mem_tool)

    llm = QwenLLM(settings)
    loop = AgentLoop(llm, registry, history=InMemoryHistory(system_prompt=SYSTEM_PROMPT))

    events = [event async for event in loop.run(f"帮我批改这句话：{essay}")]

    # 1) 主模型必须自主发起 grade_text 调用（工具名在 tool_call 事件的 call.name 上）
    tool_calls = [e for e in events if e.type == "tool_call"]
    assert any(
        getattr(getattr(e, "call", None), "name", None) == "grade_text" for e in tool_calls
    ), "真实主模型没有自主调用 grade_text，编排能力存疑"

    # 2) 工具确实执行，且收到的原文里带着要批改的句子
    assert grader.calls, "grade_text 未被实际执行"
    assert "I is" in grader.calls[0]

    # 3) 结构化结果成功回灌，主模型第二轮给短引导后正常收尾
    results = [e for e in events if e.type == "tool_result"]
    assert results and results[0].ok is True
    final = events[-1]
    assert final.type == "result"
    assert final.finish_reason.value == "final_text"
    assert final.iterations == 2
