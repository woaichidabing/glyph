"""polish_text 工具：把英文表达升档（**不改错，只求更好**）。

与 ``grade_text`` 的分工是刻意的、互补的：

- ``grade_text`` 回答"哪里错了"（对与错）；
- ``polish_text`` 回答"哪里能更好"（好与更好）。

编排（照 ``grade.py`` 的已验证形态，全部依赖以可注入协议提供）：

1. 纯本地：切句与句长分布（``writing_metrics``，0 token）；
2. 模型：一次出结构化 JSON 的升档建议（唯一花 token 的一步）；
3. 纯本地：锚点对齐 / 去重 / 限条（``polish_rules.finalize_suggestions``）
   + 升级词查本地词库定级（``polish_rules.annotate_upgrades``）。

本类只负责编排，确定性逻辑一律在 ``polish_rules``。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..runtime.errors import ToolError
from .base import ReadOnlyTool
from .polish_rules import annotate_upgrades, finalize_suggestions
from .writing_metrics import analyze_text

if TYPE_CHECKING:  # 只在类型检查时导入：工具层运行期不依赖模型层与词库实现
    from .dict_repo import DictRepository
    from .polish_analyzer import Polisher
    from .polish_models import Suggestion

__all__ = ["PolishTextTool"]

# 少于 5 词没有可升档的余地，硬给建议必然是凑数
_MIN_WORDS = 5

# 等级是 ECDICT 近似、不是官方 CEFR 词汇表：这条限定必须随结果一起交给用户
_LEVEL_APPROX_NOTE = (
    "英语等级（如「约 B2」）由本地词库依据 ECDICT 的考试标签与词频近似推算，"
    "非官方 CEFR 词汇表，仅作参考。"
)
# 模型认为没得改但没给总评时的兜底文案（保证前端不出现空白结论）
_NOTHING_TO_POLISH_OVERALL = "这段表达已经到位，没有非改不可的地方。"


class PolishTextTool(ReadOnlyTool):
    name = "polish_text"
    description = (
        "把英文的表达升档（不挑错，只求更好）：从整段里挑出最值得改进的几处（最多 5 处），"
        "每处给两个版本——一个现阶段稳妥可用、一个往上一级靠——并说明为什么更好，"
        "同时标出升级用到的词属于哪场考试。"
        "当且仅当用户明确要求把英文写得更地道、更好、更高级、换个更好的说法时才调用。"
        "不要用于：改正拼写语法错误（那是 grade_text 的活，先把错改对再谈升档）；"
        "评定作文处于什么水平（那是 analyze_writing）；"
        "也不要因为用户刚发来一段英文、或刚批改完就主动升档。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要升档的英文原文（纯文本，应该已经没有明显错误）",
                "minLength": 1,
            }
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, *, polisher: Polisher, repo: DictRepository | None) -> None:
        self._polisher = polisher
        self._repo = repo

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "")
        if not text.strip():
            raise ToolError("待升档的文本为空，无法润色")

        metrics = analyze_text(text)
        if metrics.word_count < _MIN_WORDS:
            raise ToolError(
                f"文本太短（{metrics.word_count} 词），没有可升档的余地；"
                f"请提供至少 {_MIN_WORDS} 词的完整句子或段落。"
            )

        # 取消与 ToolError 一律原样穿透：不包 try，否则主循环的协作式取消会失配
        draft = await self._polisher.polish(text, metrics)

        # 升级词的等级一律本地补，模型只给了词对
        annotated = [
            replace(sug, upgrades=annotate_upgrades(list(sug.upgrades), self._repo))
            for sug in draft.suggestions
        ]
        kept, warnings = finalize_suggestions(annotated, text)

        overall = draft.overall
        if not kept and not overall:
            overall = _NOTHING_TO_POLISH_OVERALL

        # ⚠️ 不能写成 `not kept`：建议被锚点对齐**全部剔除**（模型编了原句）时，
        # `not kept` 会把这种异常误报成"没得改"这个结论。必须以模型自报的字段为准，
        # 再与最终条数求交 —— 两个字段自洽（设计文档 §2.3）。
        return {
            "suggestions": [_dump(s) for s in kept],
            "nothing_to_polish": draft.nothing_to_polish and not kept,
            "overall": overall,
            "warnings": warnings,
            "notes": [_LEVEL_APPROX_NOTE],
        }


def _dump(sug: Suggestion) -> dict[str, Any]:
    """``Suggestion`` → 前端可用的 dict。

    字段在设计文档 §4 的契约基础上补了两样：``start`` / ``end``（§6 要求锚点对齐，
    位置必须有出口，否则前端无法在原文上高亮对照）与顶层的 ``warnings``。
    """
    return {
        "original": sug.original,
        "issue": sug.issue,
        "issue_kind": sug.issue_kind,
        "safe": sug.safe,
        "advanced": sug.advanced,
        "why": sug.why,
        "upgrades": [
            {
                "from": up.source,
                "to": up.target,
                "tags": list(up.tags),
                "level_approx": up.level_approx,
            }
            for up in sug.upgrades
        ],
        "start": sug.start,
        "end": sug.end,
    }
