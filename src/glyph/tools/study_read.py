"""read_study：让对话读到背词状态摘要。"""

from __future__ import annotations

from typing import Any

from ..runtime.errors import ToolError
from ..study import StudyStore
from .base import ReadOnlyTool

__all__ = ["ReadStudyTool"]


class ReadStudyTool(ReadOnlyTool):
    name = "read_study"

    description = (
        "当用户问“我今天背了多少词”“背词到哪了”“还有多少没背”时用。\n"
        "返回当前词书、背法、今日新学 / 复习 / 错词进度、已学卡片数。\n"
        "不要用它修改背词状态；学习动作仍由用户在做。"
    )

    parameters: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, store: StudyStore) -> None:
        self._store = store

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        try:
            state = self._store.get()
        except Exception as exc:  # noqa: BLE001 - 工具错误即数据，不炸循环
            raise ToolError(f"背词状态读不出来：{exc}") from exc
        if state is None:
            return {"hasState": False, "note": "还没有同步过背词状态。"}
        settings = state.get("settings") or {}
        queue = state.get("queue") or {}
        cards = state.get("cards") or {}
        errors = state.get("errors") or []
        free = state.get("free")
        new_words = queue.get("newWords") or []
        review_words = queue.get("reviewWords") or []
        wrong_words = queue.get("wrongWords") or []
        book_id = settings.get("bookId")
        today = queue.get("date")
        card_items = [c for c in cards.values() if isinstance(c, dict)]
        # 和背词页一致：只算当前词书、真正复习过（reps>0）的卡。
        learned = sum(
            1 for c in card_items if c.get("bookId") == book_id and (c.get("reps") or 0) > 0
        )
        due = sum(
            1
            for c in card_items
            if c.get("bookId") == book_id
            and (c.get("reps") or 0) > 0
            and today
            and str(c.get("dueDate", "")) <= str(today)
        )
        return {
            "hasState": True,
            "bookId": book_id,
            "mode": settings.get("mode"),
            "dailyNew": settings.get("dailyNew"),
            "queueDate": today,
            "today": {
                "newTotal": len(new_words),
                "newDone": queue.get("doneNew", 0),
                # 到期数按全部到期卡算，和对话上下文里的口径一致；
                # 队列切片另给 queueReviewTotal / queueReviewDone。
                "reviewTotal": due if due else len(review_words),
                "reviewDone": queue.get("doneReview", 0),
                "wrongTotal": len(wrong_words),
                "wrongDone": queue.get("doneWrong", 0),
            },
            "dueCards": due,
            "queueReviewTotal": len(review_words),
            "queueReviewDone": queue.get("doneReview", 0),
            "learnedCards": learned,
            "errorWords": len(errors),
            "free": free,
            "note": "只返回摘要；详细卡片和错词在背词页。",
        }
