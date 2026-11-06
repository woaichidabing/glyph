"""词书接口测试（从 test_server.py 拆出）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.server as srv
from web import runtime as web_runtime
from web.deps import state as web_state


class _FakeDictRepo:
    """替身：只实现词书端点用到的三个方法。"""

    def __init__(self, stats: list[dict], page: dict | None = None, available: bool = True):
        self._stats = stats
        self._page = page or {"tag": "cet6", "total": 1, "cursor": 0,
                              "next_cursor": None, "words": ["abandon"],
                              "entries": [{"word": "abandon", "phonetic": "",
                                           "pos": "v.", "gloss": "放弃"}]}
        self._available = available
        self.calls: list[tuple] = []

    def is_available(self) -> bool:
        return self._available

    def book_stats(self) -> list[dict]:
        return self._stats

    def book_words(self, tag: str, cursor: int = 0, limit: int = 600) -> dict:
        self.calls.append((tag, cursor, limit))
        return self._page


async def test_wordbook_books_lists_only_nonempty(web_env, monkeypatch) -> None:
    fake = _FakeDictRepo([
        {"tag": "zk", "label": "中考", "total": 1602},
        {"tag": "bec", "label": "BEC", "total": 0},
    ])
    monkeypatch.setattr(web_state, "dict_repo", fake)
    res = await srv.wordbook_books()
    assert res == {"books": [{"tag": "zk", "label": "中考", "total": 1602}]}


async def test_wordbook_books_503_when_db_missing(web_env, monkeypatch) -> None:
    monkeypatch.setattr(web_state, "dict_repo", _FakeDictRepo([], available=False))
    with pytest.raises(HTTPException) as ei:
        await srv.wordbook_books()
    assert ei.value.status_code == 503




def _stub_rebuild(monkeypatch):
    """把"整体重建 runtime / loop"换成桩并捕获传入的 settings。"""
    captured: dict = {}

    def _fake_build(ns):
        captured["settings"] = ns
        return (
            object(),
            web_state.registry,
            "sys",
            object(),
            None,
            None,
            object(),
            object(),
            object(),
        )

    def _fake_loop(ns, llm, reg, hist, compactor=None, compaction_observer=None):
        return SimpleNamespace(busy=False)

    monkeypatch.setattr(web_runtime, "_build_runtime", _fake_build)
    monkeypatch.setattr(web_runtime, "_make_loop", _fake_loop)
    return captured



async def test_wordbook_words_passes_pagination(web_env, monkeypatch) -> None:
    page = {"tag": "cet6", "total": 5407, "cursor": 600, "next_cursor": 1200,
            "words": ["w1", "w2"],
            "entries": [
                {"word": "w1", "phonetic": "", "pos": "n.", "gloss": "词一"},
                {"word": "w2", "phonetic": "", "pos": "v.", "gloss": "词二"},
            ]}
    fake = _FakeDictRepo([{"tag": "cet6", "label": "CET6", "total": 5407}], page=page)
    monkeypatch.setattr(web_state, "dict_repo", fake)
    res = await srv.wordbook_words("cet6", 600, 2)
    assert res == page
    assert fake.calls == [("cet6", 600, 2)]


async def test_wordbook_words_clamps_paging_params(web_env, monkeypatch) -> None:
    fake = _FakeDictRepo([{"tag": "cet6", "label": "CET6", "total": 5407}])
    monkeypatch.setattr(web_state, "dict_repo", fake)
    await srv.wordbook_words("cet6", -5, 99999)
    _, cursor, limit = fake.calls[0]
    assert cursor == 0 and limit == 2000  # 负数游标归零，limit 封顶 2000


async def test_wordbook_words_rejects_unknown_tag(web_env, monkeypatch) -> None:
    monkeypatch.setattr(web_state, "dict_repo", _FakeDictRepo([]))
    with pytest.raises(HTTPException) as ei:
        await srv.wordbook_words("nope")
    assert ei.value.status_code == 400


async def test_wordbook_words_503_when_db_missing(web_env, monkeypatch) -> None:
    monkeypatch.setattr(web_state, "dict_repo", _FakeDictRepo([], available=False))
    with pytest.raises(HTTPException) as ei:
        await srv.wordbook_words("cet6")
    assert ei.value.status_code == 503


