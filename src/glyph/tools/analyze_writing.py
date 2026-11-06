"""analyze_writing：证据驱动的英语作文 CEFR 等级分析（只读 agent 工具）。

编排（模型是最终对照者，但不做计算）：
1. ``writing_metrics`` 确定性算硬指标（句数 / 句长分布 / 复杂结构 / 可读性）；
2. ``DictRepository`` 给每个词打 CEFR 近似等级，``level_distribution`` 汇总；
3. ``LLMWritingAnalyzer`` 把原文 + 指标 + CEFR can-do 交模型对照，输出等级**区间**
   和必须对应原文的证据。

铁律：不允许跳过前两步让模型凭空给等级；等级给区间不假装精确，证据必须能在原文找到。
它**只评定、不批改**——要逐处改错请用 grade_text。
"""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import Any

from ..runtime.errors import ToolError
from .base import ReadOnlyTool
from .cefr_scale import level_distribution
from .dict_repo import DictRepository, default_dict_path
from .writing_metrics import analyze_text

__all__ = ["AnalyzeWritingTool"]

# 文本过短无法可靠分级的门槛（给区间也要有足够内容可对照）
_MIN_SENTENCES = 2
_MIN_WORDS = 15
_WORD = r"[A-Za-z]+(?:'[A-Za-z]+)?"


class AnalyzeWritingTool(ReadOnlyTool):
    name = "analyze_writing"
    description = (
        '评定一段英文写作的 CEFR 等级（A1–C2）：当且仅当用户问"我这篇作文什么水平 / 属于几级 / '
        'CEFR 多少 / 距离下一级还差什么"时才调用。参数 text 传作文或段落原文（至少 2 句、15 词，'
        "太短无法分级）。它只做等级评定并给出对应原文的证据，不逐处批改；"
        "要改错误请改用 grade_text。"
        "不要用于：用户只是让你批改、没问水平——批改是 grade_text 的事，"
        "顺手定级会把「讲解哪里错」挤成「你是什么水平」；也不要对图片转写稿定级（那是草稿不是真相）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要评定 CEFR 等级的英文作文或段落原文",
                "minLength": 1,
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, analyzer, db_path: str | None = None) -> None:
        self._analyzer = analyzer
        path = db_path if db_path is not None else default_dict_path()
        self._repo = DictRepository(path)

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text", "")).strip()
        if not text:
            raise ToolError("缺少要分析的英文文本（text）")

        metrics = analyze_text(text)
        if metrics.sentence_count < _MIN_SENTENCES or metrics.word_count < _MIN_WORDS:
            raise ToolError(
                f"文本太短（{metrics.sentence_count} 句 / {metrics.word_count} 词），"
                "无法可靠判断 CEFR 等级；请提供至少 2 句、15 词以上的完整段落。"
            )

        words = re.findall(_WORD, text)
        vocabulary = level_distribution([self._repo.word_level(w) for w in words])
        assessment = await self._analyzer.analyze(text, metrics, vocabulary)

        return {
            "level_range": list(assessment.level_range),
            "confidence": assessment.confidence,
            "evidence": [asdict(e) for e in assessment.evidence],
            "next_level_gap": assessment.next_level_gap,
            "warnings": assessment.warnings,
            "metrics": asdict(metrics),
            "vocabulary": vocabulary,
        }
