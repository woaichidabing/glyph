"""背词答后场景接口。"""

from __future__ import annotations

import json

import pytest

from glyph.examples import Example
from web.deps import state as web_state
from web.routers import study as study_api


class _Fake:
    def __init__(self, scene):
        self.scene = scene
        self.calls = []

    async def get(self, word, pos="", zh="", refresh=False):
        self.calls.append((word, pos, zh, refresh))
        return self.scene


@pytest.mark.asyncio
async def test_scene_returns_example(monkeypatch) -> None:
    fake = _Fake(
        Example(word="commit", en="They commit to it.", zh="他们投入其中。", source="tatoeba")
    )
    monkeypatch.setattr(web_state, "settings", object())
    monkeypatch.setattr(web_state, "scene_service", fake)
    res = await study_api.study_scene("commit", "v.", "投入", False)
    body = json.loads(res.body)
    assert body["scene"]["source"] == "tatoeba"
    assert fake.calls == [("commit", "v.", "投入", False)]


class _Discard:
    def __init__(self):
        self.deleted = []

    def delete(self, word):
        self.deleted.append(word)


class _FakeWithStore(_Fake):
    def __init__(self, scene):
        super().__init__(scene)
        self.store = _Discard()


@pytest.mark.asyncio
async def test_forget_scene(monkeypatch) -> None:
    fake = _FakeWithStore(None)
    monkeypatch.setattr(web_state, "settings", object())
    monkeypatch.setattr(web_state, "scene_service", fake)
    res = await study_api.forget_study_scene("commit")
    assert json.loads(res.body)["deleted"] is True
    assert fake.store.deleted == ["commit"]


@pytest.mark.asyncio
async def test_scene_returns_null(monkeypatch) -> None:
    monkeypatch.setattr(web_state, "settings", object())
    monkeypatch.setattr(web_state, "scene_service", _Fake(None))
    res = await study_api.study_scene("commit")
    assert json.loads(res.body)["scene"] is None
