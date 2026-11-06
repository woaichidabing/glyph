"""``recheck_detection`` 工具与 ``LLMRechecker`` 的离线测试（假实现，不联网，守 R6）。

这里钉死四件只有工具层才有的事（纯函数层的 N-1~N-7 由 test_recheck.py 负责）：

1. **出参可交付**：字段齐全、可 ``json.dumps``、顶层字段与 ``detection.recheck`` 逐字段相等；
2. **错误形状**：业务失败是 ``ToolError``（含人话），复核器的 ``ToolError`` 原样穿透；
3. **取消信号**：``CancelledError`` 必须原样冒出——被吞掉会破坏主循环的协作式取消；
4. **脏数据不抛错**：模型给的 ``status`` 非法时照收，交给归一化兜底成撤销。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.base import ReadOnlyTool
from glyph.tools.grading_models import Detection
from glyph.tools.recheck import (
    LLMRechecker,
    RecheckDetectionTool,
    RecheckRequest,
    RecheckVerdict,
)

TEXT = (
    "Yesterday I stayed at home. "
    "He go to school every day with his friends and his teachers. "
    "It was fine."
)

# 出参必须与 detection.recheck 同源，逐字段相等（设计文档 §1.6）
SHARED_FIELDS = (
    "status",
    "round",
    "scope",
    "corrected",
    "explanation_zh",
    "tier",
    "type",
    "note_zh",
    "reason_zh",
)
OUTPUT_KEYS = {
    "id",
    "detection",
    "status",
    "round",
    "scope",
    "corrected",
    "explanation_zh",
    "tier",
    "type",
    "note_zh",
    "reason_zh",
    "follow_up",
    "warnings",
}


def detection_arg(**over) -> dict:
    start = TEXT.index("go")
    base: dict = {
        "id": "d3",
        "original": "go",
        "corrected": "went",
        "type": "hard",
        "tier": "error",
        "source": "model",
        "start": start,
        "end": start + 2,
        "aligned": True,
        "explanation_zh": "过去的事要用过去式",
    }
    base.update(over)
    return base


def sample_request() -> RecheckRequest:
    return RecheckRequest(
        Detection(id="d3", original="go", corrected="went", source="model"),
        "He go to school every day.",
        "sentence",
        1,
    )


class FakeRechecker:
    """假复核器：按指定结论应答，或抛出指定异常（含 BaseException 用于取消用例）。"""

    def __init__(
        self,
        verdict: RecheckVerdict | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self.verdict = verdict
        self.exc = exc
        self.requests: list[RecheckRequest] = []

    async def review(self, req: RecheckRequest) -> RecheckVerdict:
        self.requests.append(req)
        if self.exc is not None:
            raise self.exc
        assert self.verdict is not None
        return self.verdict


def run_tool(rechecker: FakeRechecker, **args) -> Any:
    """跑一次工具；``tool_*`` 前缀的参数是给工具构造函数的（如 tool_max_rounds）。"""

    async def _run() -> dict:
        over = {k.removeprefix("tool_"): args.pop(k) for k in list(args) if k.startswith("tool_")}
        kwargs: dict[str, Any] = {"rechecker": rechecker, "max_rounds": 2}
        kwargs.update(over)
        base: dict[str, Any] = {"text": TEXT, "detection": detection_arg(), "round": 1}
        base.update(args)
        return await RecheckDetectionTool(**kwargs).run(base)  # type: ignore[arg-type]

    return _run()


def fake_client(content: str, finish: str = "stop") -> tuple[Any, list[dict]]:
    """假 OpenAI 客户端：返回固定内容，并记下每次调用的 payload。"""
    calls: list[dict] = []

    class _Msg:
        def __init__(self) -> None:
            self.content = content

    class _Choice:
        def __init__(self) -> None:
            self.message = _Msg()
            self.finish_reason = finish

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _Response()

    class _Chat:
        def __init__(self) -> None:
            self.completions = _Completions()

    class _Client:
        def __init__(self) -> None:
            self.chat = _Chat()

    return _Client(), calls


def failing_client(exc: BaseException) -> Any:
    """假客户端：每次调用都抛指定异常。"""

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            raise exc

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


class TestToolIdentity:
    def test_注册名与只读身份(self) -> None:
        assert RecheckDetectionTool.name == "recheck_detection"
        assert issubclass(RecheckDetectionTool, ReadOnlyTool)
        assert RecheckDetectionTool.readonly is True

    def test_入参_schema_只强制三项(self) -> None:
        params = RecheckDetectionTool.parameters

        assert params["required"] == ["text", "detection", "round"]
        assert params["additionalProperties"] is False
        assert params["properties"]["detection"]["required"] == ["id", "original", "source"]
        assert params["properties"]["detection"]["additionalProperties"] is False


class TestThreeStates:
    """三态各一例：字段齐全、follow_up 正确、与 detection.recheck 逐字段相等。"""

    @pytest.mark.parametrize(
        ("status", "follow_up"),
        [("upheld", "pending_human"), ("revised", "pending_human"), ("withdrawn", "terminal")],
    )
    async def test_三态出参齐全且同源(self, status: str, follow_up: str) -> None:
        verdict = RecheckVerdict(  # type: ignore[arg-type] - 故意喂三态之外的非法值
            status=status, reason_zh="这是理由", corrected="goes" if status == "revised" else None
        )
        out = await run_tool(FakeRechecker(verdict))

        assert set(out) == OUTPUT_KEYS
        assert out["id"] == "d3"
        assert out["status"] == status
        assert out["follow_up"] == follow_up
        assert out["round"] == 1
        assert out["detection"]["id"] == "d3"
        assert out["detection"]["recheck"] is not None
        for field in SHARED_FIELDS:
            assert out[field] == out["detection"]["recheck"][field], field

    async def test_撤销时不携带任何修改建议(self) -> None:
        verdict = RecheckVerdict(status="withdrawn", corrected="goes", tier="suggestion")
        out = await run_tool(FakeRechecker(verdict))

        assert out["corrected"] is None
        assert out["explanation_zh"] is None
        assert out["tier"] is None
        assert out["type"] is None
        assert out["detection"]["recheck"]["corrected"] is None
        assert out["note_zh"]

    async def test_维持原判回带原值(self) -> None:
        out = await run_tool(FakeRechecker(RecheckVerdict(status="upheld")))

        assert out["corrected"] == "went"
        assert out["explanation_zh"] == "过去的事要用过去式"
        assert out["tier"] == "error"
        assert out["type"] == "hard"

    async def test_改判只更新给了的字段(self) -> None:
        verdict = RecheckVerdict(status="revised", corrected="goes", tier="suggestion")
        out = await run_tool(FakeRechecker(verdict))

        assert out["corrected"] == "goes"
        assert out["tier"] == "suggestion"
        # 没给的字段沿用原值
        assert out["explanation_zh"] == "过去的事要用过去式"
        assert out["type"] == "hard"

    async def test_非法状态兜底为撤销(self) -> None:
        out = await run_tool(FakeRechecker(RecheckVerdict(status="maybe")))  # type: ignore[arg-type]

        assert out["status"] == "withdrawn"
        assert out["follow_up"] == "terminal"


class TestContextPassed:
    async def test_上下文是局部句子(self) -> None:
        rechecker = FakeRechecker(RecheckVerdict(status="upheld"))
        await run_tool(rechecker)

        req = rechecker.requests[0]
        assert req.context_text == "He go to school every day with his friends and his teachers."
        assert req.scope == "sentence"
        assert req.detection.id == "d3"
        assert req.round == 1
        assert req.note == ""

    async def test_老师备注带进请求(self) -> None:
        rechecker = FakeRechecker(RecheckVerdict(status="upheld"))
        await run_tool(rechecker, note="这里我觉得没问题")

        assert rechecker.requests[0].note == "这里我觉得没问题"

    async def test_超出硬顶时提示被记进_warnings(self) -> None:
        out = await run_tool(
            FakeRechecker(RecheckVerdict(status="upheld")), tool_max_context_chars=20
        )

        assert any("截断" in w for w in out["warnings"])

    async def test_夹带的别处条目被丢弃并记一条提示(self) -> None:
        verdict = RecheckVerdict(
            status="upheld",
            extra_raw=[{"original": "别的句子里的词", "corrected": "x"}],
        )
        out = await run_tool(FakeRechecker(verdict))

        assert any("1 条本条目之外" in w for w in out["warnings"])
        # 出参不含任何新条目：复核是复核不是重扫
        assert "detections" not in out


class TestInputGuards:
    async def test_文本为空(self) -> None:
        with pytest.raises(ToolError, match="待复核的文本为空"):
            await run_tool(FakeRechecker(RecheckVerdict(status="upheld")), text="   ")

    async def test_缺少条目编号(self) -> None:
        with pytest.raises(ToolError, match="缺少要复核的条目编号或原文片段"):
            await run_tool(
                FakeRechecker(RecheckVerdict(status="upheld")), detection=detection_arg(id="")
            )

    async def test_缺少原文片段(self) -> None:
        with pytest.raises(ToolError, match="缺少要复核的条目编号或原文片段"):
            await run_tool(
                FakeRechecker(RecheckVerdict(status="upheld")),
                detection=detection_arg(original=""),
            )

    async def test_次数用完(self) -> None:
        with pytest.raises(ToolError, match="次数已用完"):
            await run_tool(FakeRechecker(RecheckVerdict(status="upheld")), round=3)

    async def test_原文在文本里找不到(self) -> None:
        # 当初就没定位到（aligned=False）且原文已被老师改掉 → 不能拿全文去复核
        with pytest.raises(ToolError, match="找不到原文片段"):
            await run_tool(
                FakeRechecker(RecheckVerdict(status="upheld")),
                detection=detection_arg(original="根本不在这篇里", start=None, end=None),
            )

    async def test_校验失败时复核器一次都没被调用(self) -> None:
        rechecker = FakeRechecker(RecheckVerdict(status="upheld"))
        with pytest.raises(ToolError):
            await run_tool(rechecker, round=9)

        assert rechecker.requests == []


class TestErrorAndCancelPropagation:
    async def test_复核器的业务错误原样穿透(self) -> None:
        rechecker = FakeRechecker(exc=ToolError("复核模型调用失败：boom"))

        with pytest.raises(ToolError, match="复核模型调用失败"):
            await run_tool(rechecker)

    async def test_取消信号原样冒出(self) -> None:
        """这条最关键：取消被包成 ToolError 会破坏主循环的协作式取消。"""
        rechecker = FakeRechecker(exc=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await run_tool(rechecker)


class TestLLMRechecker:
    """``LLMRechecker`` 的模型应答处理（假客户端，离线）。"""

    async def test_解析出三态结论(self) -> None:
        client, calls = fake_client(
            json.dumps({"status": "revised", "reason_zh": "理由", "corrected": "goes"})
        )
        verdict = await LLMRechecker(client, "grade-model").review(sample_request())

        assert verdict.status == "revised"
        assert verdict.corrected == "goes"
        assert verdict.reason_zh == "理由"
        assert calls[0]["model"] == "grade-model"

    async def test_非法状态不抛错_原样交给归一化(self) -> None:
        client, _ = fake_client(json.dumps({"status": "不知道"}))

        verdict = await LLMRechecker(client, "m").review(sample_request())

        assert verdict.status == "不知道"  # N-1 会在归一化时兜底成 withdrawn

    async def test_夹带的数组原样收进_extra_raw(self) -> None:
        client, _ = fake_client(
            json.dumps({"status": "upheld", "detections": [{"original": "甲"}]})
        )

        verdict = await LLMRechecker(client, "m").review(sample_request())

        assert verdict.extra_raw == [{"original": "甲"}]

    async def test_代码块包裹也能解析(self) -> None:
        client, _ = fake_client('```json\n{"status": "withdrawn"}\n```')

        verdict = await LLMRechecker(client, "m").review(sample_request())

        assert verdict.status == "withdrawn"

    async def test_一直解析不出就抛_ToolError(self) -> None:
        client, calls = fake_client("这不是 JSON")

        with pytest.raises(ToolError, match="不是合法 JSON"):
            await LLMRechecker(client, "m").review(sample_request())

        assert len(calls) == 2  # 一次原始 + 一次解析重试

    async def test_调用失败包成_ToolError(self) -> None:
        with pytest.raises(ToolError, match="复核模型调用失败"):
            await LLMRechecker(failing_client(RuntimeError("connection down")), "m").review(
                sample_request()
            )

    async def test_取消不被包成_ToolError(self) -> None:
        with pytest.raises(asyncio.CancelledError):
            await LLMRechecker(failing_client(asyncio.CancelledError()), "m").review(
                sample_request()
            )


class TestOutputIsSerializable:
    async def test_出参可_json_dumps(self) -> None:
        verdict = RecheckVerdict(status="revised", corrected="goes", reason_zh="第三人称单数")
        out = await run_tool(FakeRechecker(verdict))

        assert json.loads(json.dumps(out))["status"] == "revised"

    async def test_夹带与提示也可序列化(self) -> None:
        verdict = RecheckVerdict(status="upheld", extra_raw=[{"original": "甲"}])
        out = await run_tool(FakeRechecker(verdict))

        assert json.loads(json.dumps(out))["warnings"]
