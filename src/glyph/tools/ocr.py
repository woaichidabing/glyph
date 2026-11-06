"""ocr_image 工具：把图片里的英文作文转写成可编辑文本（**只转写，绝不批改**）。

三道防污染闸里，本工具守住的是前两道：

* ① **职责闸**：出参只有 ``text`` / ``low_confidence`` / ``warnings`` / ``image_id``，
  **永不含 detections**——批改只能由 ``grade_text`` 做，两个工具互不 import；
* ③ **诚实闸**：提示词强制「认不出留 ``[?]``、禁止修正补全」，工具侧只用
  ``find_low_confidence()`` 确定性扫描占位，**自己不猜**哪里可疑。

第二道（流程闸：未经人确认的转写文本不得进批改）**不在工具里**，也**不在任何测试里**：
它靠系统词纪律 + 两段式 run 保证，属于尚未落地的一环（本轮只做转写，闸门与
系统词纪律排在后面的任务）。工具侧能离线守住的是 ① 与 ③，由
``tests/tools/test_ocr.py`` 断言。

失败语义（R5）：模型不支持视觉 / 调用失败 / 图片非法 → 一律抛 ``ToolError``，
由注册表转成 ``ok=False`` 结果回灌模型；取消信号原样传播，绝不包成 ToolError。
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

from ..runtime.errors import ToolError
from . import prompts
from .base import ReadOnlyTool, guarded
from .image import MIME_WHITELIST, ImageProvider, check_image
from .ocr_models import (
    Transcription,
    find_low_confidence,
    strip_noise_lines,
    unspell_transcription,
)

# MIME_WHITELIST / ImageProvider 现在住在 image.py（与 describe_image 共用一份口径），
# 这里 re-export 是为了不打断既有导入路径（AGENTS.md 与多处文档都按 ocr.py 引用它）。
__all__ = ["ImageProvider", "Transcriber", "LLMTranscriber", "OcrImageTool", "MIME_WHITELIST"]


class Transcriber(Protocol):
    """转写器：图片字节 → 英文文本（只抄写，不批改）。失败抛 ``ToolError``。"""

    async def transcribe(self, image_bytes: bytes, mime: str) -> str: ...


class LLMTranscriber:
    """走千问 OpenAI 兼容多模态接口的转写器。"""

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        # 转写是纯客观抄录、零判断：用温度 0 关掉"创造性"，否则高温下模型会
        # 把它认定的笔误（如 buyed）主动改对，在批改前就洗掉真实错误（#98）。
        self._temperature = temperature

    async def transcribe(self, image_bytes: bytes, mime: str) -> str:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        response = await guarded(
            self._client.chat.completions.create(
                model=self._model,
                messages=prompts.build_ocr_messages(b64, mime),
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            ),
            "视觉模型调用失败，未能转写图片",
        )

        content = getattr(response.choices[0].message, "content", None) or ""
        # 模型按提示词逐字母拼写（#98），这里确定性还原成正常文本，下游无感
        return unspell_transcription(content)


class OcrImageTool(ReadOnlyTool):
    name = "ocr_image"
    description = (
        "把用户发来的英文作文图片（截图或拍照）转写成可编辑的英文文本。"
        "只做文字抄写，不做批改、不改错、不润色；认不出的地方会标 [?]。"
        "只在用户要批改这张图里的英文时才调用；用户只是发了一张图、"
        "没说要批改时不要调用它（他只是想看懂这张图的话，那是 describe_image 的事；"
        "转写稿也必须经用户确认后才能进批改）。"
        "一次调用只转写一张图（image_id 是单个）。用户一次发来多张作文图时，"
        "要在同一轮里对每张图各调用一次 ocr_image 并行识别（不要认完一张再认下一张）；"
        "等这一批图全部转写出来、逐张交给用户确认后，再分别进入批改。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "image_id": {
                "type": "string",
                "description": "待转写的图片编号（由上传步骤给出）",
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
        transcriber: Transcriber,
        max_bytes: int,
    ) -> None:
        self._provider = provider
        self._transcriber = transcriber
        self._max_bytes = max_bytes

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        image_id = args["image_id"]
        if not image_id or not image_id.strip():
            raise ToolError("缺少图片编号（image_id），无法转写")

        image_bytes, mime = await guarded(
            self._provider.read(image_id), f"读取图片失败（image_id={image_id}）"
        )
        check_image(image_bytes, mime, self._max_bytes)

        # 转写器原则上自己抛 ToolError，但工具是最后一道防线（R5）
        raw_text = await guarded(self._transcriber.transcribe(image_bytes, mime), "图片转写失败")

        text = strip_noise_lines(raw_text)

        warnings: list[str] = []
        if not text:
            warnings.append("未能从图片中识别到英文文本，请换一张更清晰的图片或手动输入")

        low_confidence = find_low_confidence(text)
        if low_confidence:
            warnings.append(
                f"有 {len(low_confidence)} 处模型没把握，已用 [?] 标出，请对照原图重点核对"
            )

        # 职责闸：这里绝不出现 detections / 任何批改结论。
        # 出参形状由 Transcription 单点定义（R4），不在这里手拼字典。
        result = Transcription(text=text, low_confidence=low_confidence, warnings=warnings)
        return {**result.model_dump(), "image_id": image_id}
