"""grade_text 与 AgentLoop 的端到端协作测试（离线）。

验证完整 Tool-use 链路：主模型自己决定调用 grade_text → 工具并行跑（假批改器 +
真实本地拼写器）→ 结构化结果按 tool_call_id 回灌 → 主模型基于结果给一两句短引导。
主模型用 ScriptedFakeStreamingLLM 剧本，不联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.tools.grade import GradeTextTool
from glyph.tools.grading_models import Detection, GradeResult
from glyph.tools.spell import PySpellingChecker

# 复用 runtime 层的流式假模型 / 内存历史（各测试目录无 __init__，手动加入路径）
_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(_RUNTIME))
from fakes import FakeTurn, InMemoryHistory, ScriptedFakeStreamingLLM  # noqa: E402


class _FakeGrader:
    def __init__(self, result: GradeResult):
        self.result = result
        self.calls: list[str] = []

    async def grade(self, text: str) -> GradeResult:
        self.calls.append(text)
        return self.result


def _grade_tool(grader: _FakeGrader) -> GradeTextTool:
    return GradeTextTool(grader=grader, spell=PySpellingChecker())


async def _collect(run):
    return [event async for event in run]


async def test_grade_text_runs_in_loop_and_structured_result_feeds_back():
    essay = "I is a happy student."
    grader = _FakeGrader(
        GradeResult(
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
    )
    registry = ToolRegistry()
    registry.register(_grade_tool(grader))

    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("grade_text", {"text": essay})]),
            FakeTurn(text="批改好了，主要是主谓一致问题，把 I is 改成 I am，继续加油！"),
        ]
    )
    history = InMemoryHistory()
    loop = AgentLoop(llm, registry, history=history)

    events = await _collect(loop.run(f"帮我批改这句话：{essay}"))

    # 工具被真实调用，且拿到的是模型传来的原文
    assert grader.calls == [essay]

    [result_event] = [e for e in events if e.type == "tool_result"]
    assert result_event.ok is True and result_event.name == "grade_text"
    output = result_event.output
    assert output["detections"][0]["id"] == "d1"
    assert output["detections"][0]["original"] == "I is"
    assert output["stats"]["error_count"] == 1
    assert output["warnings"] == []

    # 第二轮请求模型时，结构化结果已按 id 作为 tool 消息回灌
    second_messages = llm.calls[1][0]
    tool_msgs = [m for m in second_messages if m.role == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0].is_error is False
    fed = json.loads(tool_msgs[0].content)
    assert fed["detections"][0]["original"] == "I is"
    assert fed["stats"]["error_count"] == 1

    # 账本完整：system / user / assistant(调工具) / tool(结果) / assistant(短引导)
    assert [m.role for m in history.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    final = events[-1]
    assert final.finish_reason.value == "final_text"
    assert final.iterations == 2


async def test_grade_text_in_loop_combines_model_and_real_spell_checker():
    # I is 由模型批改器发现，studdent 由真实本地拼写器发现，双路在循环里合并
    essay = "I is a studdent."
    grader = _FakeGrader(
        GradeResult(
            detections=[
                Detection(
                    original="I is",
                    corrected="I am",
                    type="hard",
                    tier="error",
                    explanation_zh="主谓不一致",
                )
            ],
            summary="ok",
        )
    )
    registry = ToolRegistry()
    registry.register(_grade_tool(grader))
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("grade_text", {"text": essay})]),
            FakeTurn(text="发现两处问题，已经标出来了。"),
        ]
    )
    loop = AgentLoop(llm, registry, history=InMemoryHistory())

    events = await _collect(loop.run(essay))
    [result_event] = [e for e in events if e.type == "tool_result"]

    detections = result_event.output["detections"]
    assert [d["original"] for d in detections] == ["I is", "studdent"]
    assert [d["source"] for d in detections] == ["model", "spell"]
    assert detections[1]["corrected"] == "student"
    assert result_event.output["stats"]["error_count"] == 2
    assert events[-1].finish_reason.value == "final_text"
