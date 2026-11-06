"""用户生词本 / 查词历史接口测试（从 test_server.py 拆出）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.server as srv
from web.deps import state as web_state

# ---------- /api/user/wordbook：生词本 + 查词历史（localStorage → 后端） ----------


@pytest.fixture()
def wordbook_env(monkeypatch, tmp_path):
    """把生词本单例换成 tmp_path 里、时钟可拨的 store。"""
    from glyph.wordbook import WordbookStore

    clock = SimpleNamespace(now=1_760_000_000_000)
    store = WordbookStore(tmp_path, clock=lambda: clock.now)
    monkeypatch.setattr(web_state, "wordbook_store", store)
    return SimpleNamespace(store=store, clock=clock)


def _wordbook_entry(word: str = "commit", translation: str = "犯（罪）；犯下") -> dict:
    return {
        "word": word,
        "phonetic": {"uk": "/kəˈmɪt/", "us": "/kəˈmɪt/"},
        "senses": [{"pos": "v.", "translation": translation, "examples": []}],
        "source": "api",
    }


async def test_wordbook_get_empty_shape(wordbook_env):
    resp = await srv.get_user_wordbook()
    assert resp == {"items": [], "history": []}


async def test_wordbook_add_returns_item_and_snapshot(wordbook_env):
    resp = await srv.add_user_wordbook(srv.WordbookAddIn(entry=_wordbook_entry()))
    assert resp["items"] == [resp["item"]]
    assert resp["history"] == []
    assert resp["item"]["word"] == "commit"
    assert resp["item"]["gloss"] == "犯（罪）；犯下"
    # 幂等：同词不同大小写再加一次，不新增、保留原记录
    wordbook_env.clock.now += 1000
    again = await srv.add_user_wordbook(
        srv.WordbookAddIn(entry=_wordbook_entry("COMMIT", "新释义"))
    )
    assert len(again["items"]) == 1
    assert again["item"]["addedAt"] == 1_760_000_000_000


async def test_wordbook_add_invalid_word_is_400(wordbook_env):
    with pytest.raises(HTTPException) as exc:
        await srv.add_user_wordbook(srv.WordbookAddIn(entry=_wordbook_entry("x" * 65)))
    assert exc.value.status_code == 400


async def test_wordbook_delete_existing_returns_removed_and_missing_returns_null(
    wordbook_env,
):
    await srv.add_user_wordbook(srv.WordbookAddIn(entry=_wordbook_entry()))
    first = await srv.delete_user_wordbook_item("COMMIT")
    assert first["removed"]["word"] == "commit"
    assert first["items"] == []

    second = await srv.delete_user_wordbook_item("not-there")
    assert second["removed"] is None
    assert second["items"] == []


async def test_wordbook_clear_items_keeps_history(wordbook_env):
    await srv.add_user_wordbook(srv.WordbookAddIn(entry=_wordbook_entry()))
    await srv.record_wordbook_lookup(srv.WordbookHistoryIn(word="commit"))

    resp = await srv.clear_user_wordbook()
    assert resp["items"] == []
    assert [h["word"] for h in resp["history"]] == ["commit"]


async def test_wordbook_restore_puts_item_back(wordbook_env):
    await srv.add_user_wordbook(srv.WordbookAddIn(entry=_wordbook_entry()))
    removed = (await srv.delete_user_wordbook_item("commit"))["removed"]

    resp = await srv.restore_user_wordbook(srv.WordbookRestoreIn(item=removed))
    assert resp["items"][0]["word"] == "commit"
    assert resp["item"]["addedAt"] == removed["addedAt"]

    # 已存在时幂等：返回原记录、不重复
    again = await srv.restore_user_wordbook(
        srv.WordbookRestoreIn(item={"word": "COMMIT", "addedAt": 1})
    )
    assert len(again["items"]) == 1
    assert again["item"]["addedAt"] == removed["addedAt"]


async def test_wordbook_history_counts_and_can_be_cleared(wordbook_env):
    first = await srv.record_wordbook_lookup(srv.WordbookHistoryIn(word="commit"))
    assert first["item"] == {"word": "commit", "at": 1_760_000_000_000, "count": 1}

    wordbook_env.clock.now += 50
    second = await srv.record_wordbook_lookup(srv.WordbookHistoryIn(word=" COMMIT "))
    assert second["item"]["count"] == 2
    assert second["item"]["at"] == 1_760_000_000_050
    assert len(second["history"]) == 1

    cleared = await srv.clear_wordbook_history()
    assert cleared["history"] == []
    assert cleared["items"] == []


async def test_wordbook_history_invalid_word_is_400(wordbook_env):
    with pytest.raises(HTTPException) as exc:
        await srv.record_wordbook_lookup(srv.WordbookHistoryIn(word="bad\nword"))
    assert exc.value.status_code == 400


async def test_wordbook_import_merges_and_reports_counts(wordbook_env):
    resp = await srv.import_user_wordbook(
        srv.WordbookImportIn(
            items=[
                {"word": "commit", "addedAt": 10, "gloss": "犯"},
                {"word": "restore", "addedAt": 20, "gloss": "恢复"},
            ],
            history=[
                {"word": "commit", "at": 30, "count": 3},
                {"word": "other", "at": 40, "count": 1},
            ],
        )
    )
    assert resp["importedItems"] == 2
    assert resp["importedHistory"] == 2
    assert [i["word"] for i in resp["items"]] == ["restore", "commit"]
    assert [h["word"] for h in resp["history"]] == ["other", "commit"]

    again = await srv.import_user_wordbook(
        srv.WordbookImportIn(
            items=[{"word": "COMMIT", "addedAt": 99}],
            history=[{"word": "commit", "at": 1, "count": 1}],
        )
    )
    assert (again["importedItems"], again["importedHistory"]) == (0, 0)
    # 合并不覆盖已有：原 addedAt / count / at 都保留较大值/原值
    assert next(i for i in again["items"] if i["word"] == "commit")["addedAt"] == 10
    commit_hist = next(h for h in again["history"] if h["word"] == "commit")
    assert commit_hist["count"] == 3 and commit_hist["at"] == 30


async def test_wordbook_import_invalid_word_is_400(wordbook_env):
    with pytest.raises(HTTPException) as exc:
        await srv.import_user_wordbook(
            srv.WordbookImportIn(items=[{"word": ""}], history=[])
        )
    assert exc.value.status_code == 400
