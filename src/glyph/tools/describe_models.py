"""describe_image 的出参模型。

与 ``ocr_models.py::Transcription`` 对称：出参形状**单点定义**（R4），
工具里不手拼字典。这样前端 / 测试 / 文档拿到的形状只有一处可改，
也避免两个看图工具一个用模型、一个用手拼字典的双重标准。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["Description"]


class Description(BaseModel):
    """看图结果：中文描述 + 告警。

    刻意**不含**任何批改字段（detections / summary / score）——
    看图就是看图，批改只由 ``grade_text`` 产出。
    """

    description: str = ""
    warnings: list[str] = Field(default_factory=list)
