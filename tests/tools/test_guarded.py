"""guarded() 的报错人话化：认得出的给解法，认不出的保留原文，取消原样穿透。"""

from __future__ import annotations

import asyncio

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.base import guarded


async def test_model_not_found_translated() -> None:
    """裸 JSON 的 model_not_found 要换成带解法的人话，模型转述时用户才知道改哪。"""

    async def boom() -> object:
        raise RuntimeError(
            "Error code: 404 - {'error': {'message': 'Model not exist.', "
            "'type': 'invalid_request_error', 'code': 'model_not_found'}}"
        )

    with pytest.raises(ToolError) as ei:
        await guarded(boom(), "视觉模型调用失败，未能转写图片")
    msg = str(ei.value)
    assert "GLYPH_VISION_MODEL" in msg
    assert "model_not_found" not in msg


async def test_unknown_error_keeps_raw_text() -> None:
    """认不出的报错原样保留——排查要用，不许吞。"""

    async def boom() -> object:
        raise RuntimeError("connection reset by peer")

    with pytest.raises(ToolError, match="connection reset by peer"):
        await guarded(boom(), "调用失败")


async def test_cancelled_passes_through() -> None:
    """取消信号绝不许包成 ToolError（R5 的硬边界）。"""

    async def cancelled() -> object:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await guarded(cancelled(), "调用失败")
