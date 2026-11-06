"""read_study 工具。"""

from __future__ import annotations

from pathlib import Path

import pytest

from glyph.study import StudyStore
from glyph.tools.study_read import ReadStudyTool


@pytest.mark.asyncio
async def test_empty(tmp_path: Path) -> None:
    got = await ReadStudyTool(StudyStore(tmp_path)).run({})
    assert got["hasState"] is False


@pytest.mark.asyncio
async def test_summary(tmp_path: Path) -> None:
    store = StudyStore(tmp_path)
    store.put(
        {
            "settings": {"bookId": "cet6", "mode": "plan", "dailyNew": 30},
            "queue": {
                "date": "2026-10-23",
                "newWords": ["a", "b"],
                "reviewWords": ["c"],
                "wrongWords": [],
                "doneNew": 1,
                "doneReview": 0,
                "doneWrong": 0,
            },
            "cards": {
                "a": {"bookId": "cet6", "reps": 2, "dueDate": "2026-10-23"},
                "b": {"bookId": "cet6", "reps": 1, "dueDate": "2026-10-25"},
                "x": {"bookId": "zk", "reps": 9, "dueDate": "2026-10-23"},
                "fresh": {"bookId": "cet6", "reps": 0, "dueDate": "2026-10-01"},
            },
            "errors": [{"word": "x"}],
            "free": None,
        }
    )
    got = await ReadStudyTool(store).run({})
    assert got["bookId"] == "cet6"
    assert got["today"]["newTotal"] == 2
    assert got["today"]["newDone"] == 1
    assert got["learnedCards"] == 2
    assert got["dueCards"] == 1
    assert got["queueReviewTotal"] == 1
    assert got["errorWords"] == 1
