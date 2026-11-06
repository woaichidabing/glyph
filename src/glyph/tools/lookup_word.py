"""lookup_word：查本地英汉词典（ECDICT）的只读 agent 工具。

用户问"这个词什么意思 / 怎么读 / 什么词性 / 时态单复数怎么变"，或模型需要先确认
词义再讲解时调用。结果直接来自本地 ECDICT，确定性、不触网。本地未收录时返回
``found=False``（这是数据、不是报错），由模型决定是否用自己的知识解释，并需向用户
说明那部分不是本地词库给的——不把模型逼到假装词库有、或瞎编。
"""

from __future__ import annotations

from typing import Any

from ..runtime.errors import ToolError
from .base import ReadOnlyTool
from .dict_repo import DictRepository, default_dict_path

__all__ = ["LookupWordTool"]


class LookupWordTool(ReadOnlyTool):
    name = "lookup_word"
    description = (
        "查询一个英文单词（或常见短语）在本地英汉词典里的音标、词性、中文释义和词形变化。"
        "当且仅当用户问某个词什么意思、怎么读、什么词性、单复数或时态怎么变，或你需要先确认"
        "词义再讲解时才调用；参数 word 传要查的词（动词用原形、名词用单数更可能命中）。"
        "结果来自本地词库；若 found=false 表示本地未收录，不要假装那是词库给的释义。"
        "不要用于：翻译整句、批改作文或讲解语法——翻译和讲解自己答，批改走 grade_text；"
        "也不要拿它替代语感判断，词典给的是释义，不是这个句子里该用哪个词。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "word": {"type": "string", "description": "要查的英文单词或短语", "minLength": 1}
        },
        "required": ["word"],
        "additionalProperties": False,
    }

    def __init__(self, repo: DictRepository | None = None) -> None:
        # repo 由装配层注入（共享同一个）；缺省时用默认路径自建，便于单独使用
        self._repo = repo or DictRepository(default_dict_path())

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        word = str(args.get("word", "")).strip()
        if not word:
            raise ToolError("缺少要查询的单词（word）")
        entry = self._repo.lookup(word)
        if entry is None:
            return {
                "found": False,
                "word": word,
                "note": (
                    "本地词库未收录该词。你可以基于自己的知识解释，但要明确告诉用户这部分"
                    "来自你的解释、不是本地词库；也可以建议换个更常见的词或先检查拼写。"
                ),
            }
        entry["found"] = True
        return entry
