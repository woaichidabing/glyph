"""grade_text 工具编排测试（离线，注入假批改器/假拼写器，经注册表端到端）。

覆盖：正常合并编号统计、全对文本、批改器失败（整工具失败）、
拼写器失败（局部降级，模型结果照常）、空文本被参数校验挡住、两路真并行。
"""

import asyncio
import time

import pytest

from glyph.runtime.errors import ToolError
from glyph.runtime.registry import ToolRegistry
from glyph.tools.grade import GradeTextTool
from glyph.tools.grading_models import Detection, GradeResult


class FakeGrader:
    def __init__(self, result=None, raises=None, delay=0.0):
        self.result, self.raises, self.delay = result, raises, delay
        self.calls: list[str] = []

    async def grade(self, text: str) -> GradeResult:
        self.calls.append(text)
        if self.delay:
            await __import__("asyncio").sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.result


class FakeSpell:
    def __init__(self, detections=None, raises=None, delay=0.0):
        self.detections, self.raises, self.delay = detections or [], raises, delay
        self.calls: list[str] = []

    async def check(self, text: str):
        self.calls.append(text)
        if self.delay:
            await __import__("asyncio").sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.detections


def _registry(grader, spell) -> tuple[ToolRegistry, GradeTextTool]:
    tool = GradeTextTool(grader=grader, spell=spell)
    registry = ToolRegistry()
    registry.register(tool)
    return registry, tool


async def test_grade_text_merges_numbers_and_stats():
    essay = "I is a studdent."
    grader = FakeGrader(
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
            summary="基础不错，注意主谓一致",
        )
    )
    spell_start = essay.index("studdent")
    spell = FakeSpell(
        [
            Detection(
                original="studdent",
                corrected="student",
                source="spell",
                start=spell_start,
                end=spell_start + len("studdent"),
            )
        ]
    )
    registry, _ = _registry(grader, spell)

    result = await registry.execute("grade_text", {"text": essay})

    assert result.ok is True
    dets = result.output["detections"]
    assert [d["id"] for d in dets] == ["d1", "d2"]
    assert [d["original"] for d in dets] == ["I is", "studdent"]
    assert dets[0]["start"] == 0 and essay[dets[0]["start"] : dets[0]["end"]] == "I is"
    assert result.output["stats"] == {
        "words": 4,
        "error_count": 2,
        "suggestion_count": 0,
        "total": 2,
    }
    assert result.output["summary"] == "基础不错，注意主谓一致"
    assert result.output["warnings"] == []
    # 两路都被调用，且拿到的是同一份原文
    assert grader.calls == [essay] and spell.calls == [essay]


async def test_grade_text_clean_essay_empty_detections():
    grader = FakeGrader(GradeResult(detections=[], summary="写得很地道，没有明显问题"))
    registry, _ = _registry(grader, FakeSpell())

    result = await registry.execute("grade_text", {"text": "If I were rich, I would travel."})

    assert result.ok is True
    assert result.output["detections"] == []
    assert result.output["stats"]["total"] == 0
    assert "没有明显问题" in result.output["summary"]


async def test_grade_text_grader_failure_fails_whole_tool():
    grader = FakeGrader(raises=ToolError("批改模型调用失败：500"))
    spell = FakeSpell([Detection(original="x", corrected="y", source="spell", start=0, end=1)])
    registry, _ = _registry(grader, spell)

    result = await registry.execute("grade_text", {"text": "x"})

    assert result.ok is False
    assert "批改" in result.error


async def test_grade_text_spell_failure_degrades_but_keeps_model_result():
    grader = FakeGrader(
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
            summary="总体可以",
        )
    )
    spell = FakeSpell(raises=RuntimeError("dictionary missing"))
    registry, _ = _registry(grader, spell)

    result = await registry.execute("grade_text", {"text": "I is a student."})

    assert result.ok is True  # 拼写挂了不拖垮整个工具
    assert [d["original"] for d in result.output["detections"]] == ["I is"]
    assert any("拼写" in w for w in result.output["warnings"])


async def test_grade_text_empty_string_rejected_by_schema():
    registry, _ = _registry(FakeGrader(GradeResult()), FakeSpell())

    result = await registry.execute("grade_text", {"text": ""})

    assert result.ok is False


async def test_grade_text_runs_spell_and_grader_in_parallel():
    # 两路各 0.2s：串行要约 0.4s，并行应明显更少
    grader = FakeGrader(GradeResult(detections=[], summary="ok"), delay=0.2)
    spell = FakeSpell(delay=0.2)
    registry, _ = _registry(grader, spell)

    start = time.perf_counter()
    result = await registry.execute("grade_text", {"text": "I am happy."})
    elapsed = time.perf_counter() - start

    assert result.ok is True
    assert elapsed < 0.35


class _SelfCancellingGrader:
    """模拟子任务内部收到取消（非外层 task.cancel）：CancelledError 必须原样传播。"""

    async def grade(self, text: str):
        raise asyncio.CancelledError()


class _SelfCancellingSpell:
    async def check(self, text: str):
        raise asyncio.CancelledError()


async def test_grader_cancellation_propagates_instead_of_toolerror():
    tool = GradeTextTool(grader=_SelfCancellingGrader(), spell=FakeSpell())

    # 绝不能把取消包装成 ToolError，否则主循环会当成工具失败而非中止
    with pytest.raises(asyncio.CancelledError):
        await tool.run({"text": "I am happy."})


async def test_spell_cancellation_propagates_instead_of_silent_degrade():
    grader = FakeGrader(GradeResult(detections=[], summary="ok"))
    tool = GradeTextTool(grader=grader, spell=_SelfCancellingSpell())

    # 绝不能把拼写路的取消当成"拼写不可用"静默降级成功
    with pytest.raises(asyncio.CancelledError):
        await tool.run({"text": "I am happy."})
