"""ocr_image 工具的离线测试（不联网，R6）。

盯三件事：① 职责闸——出参**永不含 detections**；② 失败统一 ``ToolError``（R5）；
③ 取消信号原样传播，绝不包成 ToolError。真视觉模型调用见 test_ocr_integration.py。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.ocr import LLMTranscriber, OcrImageTool

SAMPLE = "Last weekend was happy. On Saturday morning I go to park."
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class FakeProvider:
    def __init__(
        self, data: bytes = JPEG, mime: str = "image/jpeg", *, error: Exception | None = None
    ):
        self._data = data
        self._mime = mime
        self._error = error
        self.seen_ids: list[str] = []

    async def read(self, image_id: str) -> tuple[bytes, str]:
        self.seen_ids.append(image_id)
        if self._error:
            raise self._error
        return self._data, self._mime


class FakeTranscriber:
    def __init__(self, text: str = SAMPLE, *, error: Exception | None = None, cancel: bool = False):
        self._text = text
        self._error = error
        self._cancel = cancel
        self.seen_mimes: list[str] = []

    async def transcribe(self, image_bytes: bytes, mime: str) -> str:
        self.seen_mimes.append(mime)
        if self._cancel:
            raise asyncio.CancelledError()
        if self._error:
            raise self._error
        return self._text


def make_tool(
    provider: FakeProvider | None = None,
    transcriber: FakeTranscriber | None = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
) -> tuple[OcrImageTool, FakeProvider, FakeTranscriber]:
    p = provider or FakeProvider()
    t = transcriber or FakeTranscriber()
    return OcrImageTool(provider=p, transcriber=t, max_bytes=max_bytes), p, t


class TestHappyPath:
    async def test_出参四个字段且回带_image_id(self) -> None:
        tool, provider, transcriber = make_tool()
        out = await tool.run({"image_id": "img-1"})

        assert out["text"] == SAMPLE
        assert out["low_confidence"] == []
        assert out["warnings"] == []
        assert out["image_id"] == "img-1"
        assert provider.seen_ids == ["img-1"]
        assert transcriber.seen_mimes == ["image/jpeg"]

    async def test_职责闸_出参不含_detections(self) -> None:
        tool, _, _ = make_tool()
        out = await tool.run({"image_id": "img-1"})

        assert "detections" not in out
        assert "summary" not in out

    async def test_占位被标成低置信片段(self) -> None:
        tool, _, _ = make_tool(transcriber=FakeTranscriber("I went to [?] park."))
        out = await tool.run({"image_id": "img-1"})

        assert len(out["low_confidence"]) == 1
        assert out["low_confidence"][0]["start"] == 10
        assert any("[?]" in w for w in out["warnings"])

    async def test_模型输出为空时给可读提示(self) -> None:
        tool, _, _ = make_tool(transcriber=FakeTranscriber("   "))
        out = await tool.run({"image_id": "img-1"})

        assert out["text"] == ""
        assert any("未能从图片中识别到英文文本" in w for w in out["warnings"])

    async def test_降噪_剔除非英文行(self) -> None:
        # 冒烟实测：模型偶尔会把中文界面文案混进输出，这类行连一个拉丁字母都没有
        tool, _, _ = make_tool(transcriber=FakeTranscriber(f"{SAMPLE}\n今日总结：\n1/1"))
        out = await tool.run({"image_id": "img-1"})

        assert out["text"] == SAMPLE
        assert "今日总结" not in out["text"]


class TestFailures:
    async def test_空图片编号报错(self) -> None:
        tool, _, _ = make_tool()

        with pytest.raises(ToolError, match="缺少图片编号"):
            await tool.run({"image_id": "  "})

    async def test_不支持的格式报错(self) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(mime="application/pdf"))

        with pytest.raises(ToolError, match="不支持的图片格式"):
            await tool.run({"image_id": "img-1"})

    async def test_超出字节上限报错(self) -> None:
        tool, _, _ = make_tool(max_bytes=10)

        with pytest.raises(ToolError, match="图片过大"):
            await tool.run({"image_id": "img-1"})

    async def test_空图片报错(self) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(data=b""))

        with pytest.raises(ToolError, match="图片内容为空"):
            await tool.run({"image_id": "img-1"})

    async def test_取图失败转成工具错误(self) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(error=FileNotFoundError("no such file")))

        with pytest.raises(ToolError, match="读取图片失败"):
            await tool.run({"image_id": "img-1"})

    async def test_模型失败转成工具错误(self) -> None:
        tool, _, _ = make_tool(transcriber=FakeTranscriber(error=RuntimeError("boom")))

        with pytest.raises(ToolError, match="图片转写失败"):
            await tool.run({"image_id": "img-1"})

    async def test_取消信号原样传播_不包成工具错误(self) -> None:
        tool, _, _ = make_tool(transcriber=FakeTranscriber(cancel=True))

        with pytest.raises(asyncio.CancelledError):
            await tool.run({"image_id": "img-1"})

    async def test_取图时取消也原样传播(self) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(error=asyncio.CancelledError()))

        with pytest.raises(asyncio.CancelledError):
            await tool.run({"image_id": "img-1"})


class TestLLMTranscriberCall:
    """转写是客观抄录、零判断：必须以确定性温度调用，杜绝高温下的创造性纠错。"""

    async def test_转写以温度0调用视觉模型(self) -> None:
        captured: dict = {}

        class _Completions:
            async def create(self, **kwargs):
                captured.update(kwargs)
                message = types.SimpleNamespace(content="I buyed two apple.")
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        class _Chat:
            completions = _Completions()

        client = types.SimpleNamespace(chat=_Chat())
        text = await LLMTranscriber(client, "qwen3-vl-flash").transcribe(b"", "image/jpeg")

        assert text == "I buyed two apple."
        assert captured.get("temperature") == 0

    async def test_逐字母输出被确定性还原(self) -> None:
        class _Completions:
            async def create(self, **kwargs):
                message = types.SimpleNamespace(content="b-u-y-e-d   t-w-o .")
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        class _Chat:
            completions = _Completions()

        client = types.SimpleNamespace(chat=_Chat())
        text = await LLMTranscriber(client, "qwen3-vl-flash").transcribe(b"", "image/jpeg")

        assert text == "buyed two."
