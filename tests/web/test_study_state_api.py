"""背词状态接口。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from glyph.study import StudyStore
from web.deps import state as web_state
from web.routers import study as study_api


@pytest.fixture()
def study_env(monkeypatch, tmp_path: Path) -> StudyStore:
    store = StudyStore(tmp_path)
    monkeypatch.setattr(web_state, "study_store", store)
    return store


@pytest.mark.asyncio
async def test_state_empty_then_saved(study_env: StudyStore) -> None:
    empty = await study_api.get_study_state()
    assert json.loads(empty.body)["state"] is None
    await study_api.put_study_state({"settings": {"bookId": "cet6"}})
    got = await study_api.get_study_state()
    assert json.loads(got.body)["state"]["settings"]["bookId"] == "cet6"
