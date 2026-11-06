"""describe_image 工具的离线测试（不联网，R6）。

为什么要有这个工具（2026-09-25 实测取证）：
主模型 deepseek-v4-pro 是**纯文本**的，看不见图——而且面对看不见的图会**间歇性编造**
（同一张图问两次，一次编出一段毫不相干的"流程图"，一次才老实说看不见）。
所以用户发图但不是要批改时（题目、单词表、课本页、招牌），必须有一条**真的能看图**
的通道，否则主模型只能靠编。

它是「轻量看图入口」，跟 ocr_image 的分工：
- ocr_image：为了拿到英文原文去**批改**（转写，产出原文，绑在批改链路里）
- describe_image：为了**看懂**这张图是什么（解释，产出中文描述，不做批改）

这里盯四件事：① 出参永不含批改字段（detections / summary / score，职责闸）；
② 失败统一 ToolError（R5）；③ 取消信号原样传播；④ 继承 ReadOnlyTool（R10）。
"""

from __future__ import annotations

import asyncio

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.base import ReadOnlyTool
from glyph.tools.describe import DescribeImageTool, Describer
from glyph.tools.image import MIME_WHITELIST
from glyph.tools.ocr import MIME_WHITELIST as OCR_MIME_WHITELIST

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
SAMPLE_DESC = "这是一道英语选择题的截图，题目问的是时态。"


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


class FakeDescriber:
    def __init__(
        self, text: str = SAMPLE_DESC, *, error: Exception | None = None, cancel: bool = False
    ):
        self._text = text
        self._error = error
        self._cancel = cancel
        self.seen_mimes: list[str] = []

    async def describe(self, image_bytes: bytes, mime: str) -> str:
        self.seen_mimes.append(mime)
        if self._cancel:
            raise asyncio.CancelledError()
        if self._error:
            raise self._error
        return self._text


def make_tool(
    provider: FakeProvider | None = None,
    describer: FakeDescriber | None = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
) -> tuple[DescribeImageTool, FakeProvider, FakeDescriber]:
    p = provider or FakeProvider()
    d = describer or FakeDescriber()
    return DescribeImageTool(provider=p, describer=d, max_bytes=max_bytes), p, d


class TestHappyPath:
    async def test_出参带中文描述并回带_image_id(self) -> None:
        tool, provider, describer = make_tool()
        out = await tool.run({"image_id": "img-1"})

        assert out["description"] == SAMPLE_DESC
        assert out["image_id"] == "img-1"
        assert out["warnings"] == []
        assert provider.seen_ids == ["img-1"]
        assert describer.seen_mimes == ["image/jpeg"]

    @pytest.mark.parametrize("forbidden", ["detections", "summary", "score"])
    async def test_职责闸_出参永不含批改字段(self, forbidden: str) -> None:
        """跟 ocr_image 同源：看图工具绝不产出批改结论，批改只由 grade_text 做。

        三个字段逐个参数化（不是一个用例里写三条断言）：漏守哪一个，红的时候
        就指名哪一个。其中 `score` 是 2026-09-25 验证时抓出来的**无守护承诺**——
        文档里写了"永不含 score"，但当时没人断言它。
        """
        tool, _, _ = make_tool()
        out = await tool.run({"image_id": "img-1"})

        assert forbidden not in out


class TestReadOnlyIdentity:
    def test_继承_ReadOnlyTool(self) -> None:
        assert issubclass(DescribeImageTool, ReadOnlyTool)
        assert DescribeImageTool.readonly is True

    def test_参数是_image_id_且不允许多传(self) -> None:
        assert DescribeImageTool.name == "describe_image"
        assert list(DescribeImageTool.parameters["properties"]) == ["image_id"]
        assert DescribeImageTool.parameters["additionalProperties"] is False


class TestInputValidation:
    async def test_缺少_image_id_报_ToolError(self) -> None:
        tool, _, _ = make_tool()
        with pytest.raises(ToolError):
            await tool.run({"image_id": "   "})

    @pytest.mark.parametrize("mime", ["image/gif", "application/pdf", "text/plain"])
    async def test_不支持的格式报_ToolError(self, mime: str) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(mime=mime))
        with pytest.raises(ToolError):
            await tool.run({"image_id": "img-1"})

    def test_白名单与_ocr_共用同一份(self) -> None:
        """必须是**同一个对象**，不是"内容相同的两份"。

        只断言内容相等的话，有人给 describe 单独弄一份白名单照样绿 ——
        那正是这条要防的漂移：同一张图，一个工具能看、另一个不能，最难查。
        """
        assert OCR_MIME_WHITELIST is MIME_WHITELIST
        assert "image/jpeg" in MIME_WHITELIST
        assert "image/gif" not in MIME_WHITELIST

    async def test_图片过大报_ToolError(self) -> None:
        tool, _, _ = make_tool(max_bytes=10)
        with pytest.raises(ToolError):
            await tool.run({"image_id": "img-1"})


class TestFailureSemantics:
    async def test_读图失败统一成_ToolError(self) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(error=RuntimeError("磁盘没了")))
        with pytest.raises(ToolError):
            await tool.run({"image_id": "img-1"})

    async def test_视觉模型失败统一成_ToolError(self) -> None:
        tool, _, _ = make_tool(describer=FakeDescriber(error=RuntimeError("模型超时")))
        with pytest.raises(ToolError):
            await tool.run({"image_id": "img-1"})

    async def test_取消信号原样传播_绝不包成_ToolError(self) -> None:
        """取消是控制信号不是错误；包成 ToolError 会让 loop 收不到取消。"""
        tool, _, _ = make_tool(describer=FakeDescriber(cancel=True))
        with pytest.raises(asyncio.CancelledError):
            await tool.run({"image_id": "img-1"})

    async def test_读图被取消也原样传播(self) -> None:
        tool, _, _ = make_tool(provider=FakeProvider(error=asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await tool.run({"image_id": "img-1"})


class TestEmptyResult:
    async def test_模型没说出内容时给可操作的警告(self) -> None:
        tool, _, _ = make_tool(describer=FakeDescriber(text="   "))
        out = await tool.run({"image_id": "img-1"})

        assert out["description"] == ""
        assert any("换一张" in w for w in out["warnings"])


def test_describer_protocol_shape() -> None:
    """Describer 是协议而非具体类——装配层可以换视觉模型而不动工具。"""
    assert hasattr(Describer, "describe")
