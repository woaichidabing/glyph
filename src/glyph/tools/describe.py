"""describe_image 工具：看懂一张图（**只看，绝不批改**）。

它和 ``ocr_image`` 调的是同一个视觉模型，但契约完全不同：

* ``ocr_image`` —— 为了拿到英文原文去**批改**：只抄字，产出原文，绑在批改链路里；
* ``describe_image`` —— 为了**看懂**这张图是什么：产出中文描述，不做批改。

为什么必须有它（2026-09-25 实测取证）：主模型 deepseek-v4-pro 是**纯文本**的，
看不见图；而且面对看不见的图会**间歇性编造** —— 同一张图问两次，一次编出一段
毫不相干的"流程图"，一次才老实说看不见。没有这条通道，用户发一张图问
"这是什么"时，主模型只能靠编。

失败语义（R5）：读图失败 / 模型失败 / 图片非法 → 一律抛 ``ToolError``，
由注册表转成 ``ok=False`` 回灌模型；取消信号原样传播，绝不包成 ToolError。
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from ..runtime.errors import ToolError
from . import prompts
from .base import ReadOnlyTool, guarded
from .describe_models import Description
from .image import ImageProvider, check_image

__all__ = ["Describer", "LLMDescriber", "DescribeImageTool"]


class Describer(Protocol):
    """看图器：图片字节 → 中文描述（只看，不批改）。失败抛 ``ToolError``。"""

    async def describe(self, image_bytes: bytes, mime: str) -> str: ...


class LLMDescriber:
    """走千问 OpenAI 兼容多模态接口的看图器。"""

    def __init__(self, client: Any, model: str, *, max_tokens: int = 1024) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    async def describe(self, image_bytes: bytes, mime: str) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        response = await guarded(
            self._client.chat.completions.create(
                model=self._model,
                messages=prompts.build_describe_messages(b64, mime),
                max_tokens=self._max_tokens,
            ),
            "视觉模型调用失败，未能看懂图片",
        )

        content = getattr(response.choices[0].message, "content", None) or ""
        return content.strip()


class DescribeImageTool(ReadOnlyTool):
    name = "describe_image"
    description = (
        "看一眼用户发来的图片，用中文说清图里是什么；图里有英文或中文就把原文原样抄出来。"
        "只看不批：不做批改、不改错、不润色，也不给学习建议。"
        "当且仅当用户发了一张图、想让你看懂或解释这张图（而不是要批改里面的英文）时调用。"
        "不要用于：要批改图里的英文——那是 ocr_image 的事。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_id": {
                "type": "string",
                "description": "待查看的图片编号（由上传步骤给出）",
                "minLength": 1,
            }
        },
        "required": ["image_id"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        provider: ImageProvider,
        describer: Describer,
        max_bytes: int,
    ) -> None:
        self._provider = provider
        self._describer = describer
        self._max_bytes = max_bytes

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        image_id = args["image_id"]
        if not image_id or not image_id.strip():
            raise ToolError("缺少图片编号（image_id），无法查看图片")

        image_bytes, mime = await guarded(
            self._provider.read(image_id), f"读取图片失败（image_id={image_id}）"
        )
        check_image(image_bytes, mime, self._max_bytes)

        raw = await guarded(self._describer.describe(image_bytes, mime), "图片识别失败")
        description = raw.strip()

        warnings: list[str] = []
        if not description:
            warnings.append("没能看懂这张图，请换一张更清晰的图片，或把你的问题写清楚")

        # 职责闸：这里绝不出现 detections / 任何批改结论。
        # 出参形状由 Description 单点定义（R4），不在这里手拼字典 ——
        # 与 OcrImageTool 用 Transcription 是同一条规矩。
        result = Description(description=description, warnings=warnings)
        return {**result.model_dump(), "image_id": image_id}
