"""propose_wordbook_add：把“要不要收进生词本”做成一张可点的卡（只读工具）。

为什么需要它：用户问完一个词，主脑能解释，却没有任何办法把词交给用户**确认收藏**。
这个工具只做三件事——查本地词库、算他查过几次、把卡片需要的字段结构化交出来；
**它绝不写生词本**。真正落库只发生在用户点卡片上的“收进生词本”之后（走现有
/api/user/wordbook 接口），所以工具不能声称“已经收好了”。

R5：参数非法（空词 / 超长）抛 ToolError；本地未收录 / 没有释义 / 已收藏都是
**数据**，返回 proposed=False 让主脑换一种说法，不递卡。
"""

from __future__ import annotations

from typing import Any

from ..runtime.errors import ToolError
from ..wordbook import WordbookStore, validate_word
from .base import ReadOnlyTool
from .dict_repo import DictRepository, default_dict_path

__all__ = ["ProposeWordbookAddTool"]

_GLOSS_LIMIT = 80


def _clip(text: str) -> str:
    clean = " ".join(text.split())
    if len(clean) <= _GLOSS_LIMIT:
        return clean
    return clean[:_GLOSS_LIMIT].rstrip() + "…"


def _first_gloss(entry: dict[str, Any]) -> tuple[str, str]:
    """词条 → (词性, 中文/英文首义)。中文缺失时退回英文 definition。"""
    senses = entry.get("senses")
    if isinstance(senses, list):
        for sense in senses:
            if not isinstance(sense, dict):
                continue
            translation = str(sense.get("translation") or "").strip()
            if translation:
                return str(sense.get("pos") or "").strip(), _clip(translation)
    definitions = entry.get("definitions")
    if isinstance(definitions, list):
        for definition in definitions:
            text = str(definition or "").strip()
            if text:
                return "", _clip(text)
    return "", ""


class ProposeWordbookAddTool(ReadOnlyTool):
    """只读：把收词卡需要的词、音标、释义和查词次数交给界面。"""

    name = "propose_wordbook_add"

    description = (
        "用户可能想把这个英文词收进生词本时，递一张收词确认卡。"
        "当且仅当用户明确说“收进生词本 / 帮我记一下这个词”，或你刚讲完一个词、"
        "他表现出想记（查过、怕忘、说老是记不住）时才调用；参数 word 只传一个词或短语。"
        "工具只查本地词库，把词 / 音标 / 释义 / 查过几次交给界面，**不写生词本**；"
        "用户点卡片上的“收进生词本”才算收，你绝不能替他点、也不要在回复里说已经收好了。"
        "不要用于：只是问词义却没想记、每讲一个词就弹一次、批改 / 翻译 / 规划，"
        "或这个词已经在生词本里——这些都不要调用。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "word": {
                "type": "string",
                "description": "要提议收进生词本的一个英文单词或短语",
                "minLength": 1,
                "maxLength": 64,
            }
        },
        "required": ["word"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        repo: DictRepository | None = None,
        store: WordbookStore | None = None,
    ) -> None:
        # repo / store 由装配层注入（与词书接口共享同一份）；缺省时按默认路径自建，便于单独使用
        self._repo = repo or DictRepository(default_dict_path())
        self._store = store

    def _state(self, word: str) -> tuple[int, bool]:
        """(查词次数, 是否已收藏)。历史读不到只降级为 0 次，不影响递卡。"""
        if self._store is None:
            return 0, False
        try:
            snapshot = self._store.snapshot()
        except Exception:
            # 只读旁路：历史文件坏了 / 盘暂时读不到，卡片仍可用，只是不显示次数；
            # 不写任何状态，也不把一次收词机会变成整轮失败。
            return 0, False
        key = word.casefold()
        collected = any(
            isinstance(item, dict) and str(item.get("word") or "").casefold() == key
            for item in snapshot.get("items") or []
        )
        count = 0
        for entry in snapshot.get("history") or []:
            if isinstance(entry, dict) and str(entry.get("word") or "").casefold() == key:
                raw = entry.get("count")
                count = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
                break
        return count, collected

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = str(args.get("word", "")).strip()
        try:
            word = validate_word(raw)
        except ValueError as exc:
            raise ToolError(f"{exc}。word 只传一个词或短语，再调一次。") from exc

        entry = self._repo.lookup(word)
        if entry is None:
            return {
                "proposed": False,
                "status": "not_found",
                "word": word,
                "summary": (
                    "本地词库没收录这个词，不要递收词卡。你可以基于自己的知识解释，"
                    "但要说明这不是本地词库给的；也可以建议他核对拼写或换一个更常见的词。"
                ),
            }

        canonical = str(entry.get("word") or word).strip() or word
        pos, gloss = _first_gloss(entry)
        if not gloss:
            return {
                "proposed": False,
                "status": "no_gloss",
                "word": canonical,
                "summary": (
                    "词库有这个词，但没有可用的释义，不要递收词卡。"
                    "按自己的知识解释，并说明不是本地词库给的。"
                ),
            }

        count, collected = self._state(canonical)
        if collected:
            return {
                "proposed": False,
                "status": "already_collected",
                "word": canonical,
                "summary": (
                    "这个词已经在生词本里，不要重复递卡，也不要说“又收了一次”；"
                    "需要的话直接讲这个词。"
                ),
            }

        raw_phonetic = entry.get("phonetic")
        phonetic: dict[str, str] = {"uk": "", "us": ""}
        if isinstance(raw_phonetic, dict):
            phonetic = {
                "uk": str(raw_phonetic.get("uk") or "").strip(),
                "us": str(raw_phonetic.get("us") or "").strip(),
            }

        result: dict[str, Any] = {
            "proposed": True,
            "status": "proposed",
            "word": canonical,
            "phonetic": phonetic,
            "pos": pos,
            "gloss": gloss,
            "lookedUpCount": count,
            "summary": (
                "收词卡已经交给界面，等用户自己点“收进生词本”。"
                "不要替他点，也不要说已经收了；最多说一句“卡片在下面”，然后停。"
            ),
        }
        queried = entry.get("queried_form")
        if isinstance(queried, str):
            queried_clean = queried.strip()
            if queried_clean and queried_clean.casefold() != canonical.casefold():
                result["queriedForm"] = queried_clean
        return result
