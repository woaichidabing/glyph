"""grade_text 工具：英语作文/句子的纯文本批改（第一版不含图片 OCR）。

编排（全部能力以可注入的协议提供，便于测试与替换）：

1. 并行执行「本地拼写检查（离线、0 token）」与「批改模型（语法/时态/中式英语/
   地道性，一次出结构化 JSON）」；
2. 纯本地合并清洗：原文锚点对齐、去重叠、建议限量（硬错误不限量）、编号、统计（merge.py）；
3. 失败分级：批改主力挂了 → 整个工具失败（ToolError，让主模型知道没批成）；
   拼写挂了 → 局部降级，只返回模型结果并给 warning，不拖垮批改。

位置对齐、建议限量等确定性逻辑不在这里，全部在 merge.py，本类只负责编排。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..runtime.errors import ToolError
from .base import ReadOnlyTool
from .grader import Grader
from .merge import finalize_detections
from .spell import SpellingChecker


class GradeTextTool(ReadOnlyTool):
    name = "grade_text"
    description = (
        "批改英文文本（作文或句子）：并行做本地拼写检查和模型批改，覆盖语法、时态、"
        "主谓一致、冠词介词、中式英语与表达地道性，返回带原文位置的逐条修改建议、"
        "简体中文讲解和一句总评。当且仅当用户要求批改、修改、检查**成篇的**英语作文时调用；"
        "单句/短句的语法问题自己讲就行，不要为了一句话调用这个工具。"
        "不要用于：讲解语法规则、翻译、造句、词汇辨析——这类自己答就行；"
        "也不要拿未经用户确认的转写稿来批改。"
        "返回后只说一句短的（批了几处、在哪处理）；逐条条目由界面卡片展示，"
        "不要复述条目、不要重贴原文或改后全文、不要问他要不要继续。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要批改的英文原文（纯文本，不含图片）",
                "minLength": 1,
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, *, grader: Grader, spell: SpellingChecker) -> None:
        self._grader = grader
        self._spell = spell

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        text = args["text"]
        if not text or not text.strip():
            raise ToolError("待批改文本为空，无法批改")

        # allSettled 风格：任一路失败都先拿到结果，再决定整失败还是局部降级
        grade_result, spell_result = await asyncio.gather(
            self._grader.grade(text),
            self._spell.check(text),
            return_exceptions=True,
        )

        # 取消信号优先级最高：任一路被取消都必须原样传播，绝不能包成 ToolError
        # 或当成"拼写不可用"静默降级，否则主循环的协作式取消会被吞掉、状态不一致。
        if isinstance(grade_result, asyncio.CancelledError):
            raise grade_result
        if isinstance(spell_result, asyncio.CancelledError):
            raise spell_result

        if isinstance(grade_result, ToolError):
            raise grade_result
        if isinstance(grade_result, BaseException):
            raise ToolError(f"批改失败，暂时无法完成模型批改：{grade_result}") from grade_result

        warnings: list[str] = []
        if isinstance(spell_result, BaseException):
            spell_detections = []
            warnings.append("本地拼写检查暂时不可用，本次仅使用模型批改")
        else:
            spell_detections = spell_result

        detections, stats, cap_warnings = finalize_detections(
            list(grade_result.detections), list(spell_detections), text
        )
        warnings.extend(cap_warnings)

        return {
            "detections": [d.model_dump() for d in detections],
            "summary": grade_result.summary,
            # 四维评分（内容/结构/准确/丰富）；模型没给时为 None，前端不渲染分数区
            "scores": grade_result.scores.model_dump() if grade_result.scores else None,
            "stats": stats,
            "warnings": warnings,
        }
