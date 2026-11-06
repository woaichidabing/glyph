"""例句服务：缓存优先，Tatoeba 次之，模型兜底。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from glyph.examples import Example, ExampleStore, SceneService
from glyph.examples.tatoeba import pick_example


def _run(service: SceneService, word: str, **kwargs):
    return asyncio.run(service.get(word, **kwargs))


def test_store_delete(tmp_path: Path) -> None:
    store = ExampleStore(tmp_path)
    store.put(
        Example(word="commit", en="They commit to it.", zh="他们投入其中。", source="tatoeba")
    )
    assert store.get("commit") is not None
    store.delete("commit")
    assert store.get("commit") is None


def test_cache_is_used_before_providers(tmp_path: Path) -> None:
    store = ExampleStore(tmp_path)
    store.put(
        Example(word="commit", en="She will commit to it.", zh="她会投入这件事。", source="tatoeba")
    )
    calls = []

    async def fetcher(word: str):
        calls.append(word)
        return None

    service = SceneService(None, store=store, fetcher=fetcher)
    got = _run(service, "commit")
    assert got is not None and got.source == "tatoeba"
    assert calls == []


def test_tatoeba_wins_and_is_cached(tmp_path: Path) -> None:
    store = ExampleStore(tmp_path)
    calls = []

    async def fetcher(word: str):
        calls.append(word)
        return Example(
            word=word, en="They commit to the plan.", zh="他们投入这个计划。", source="tatoeba"
        )

    async def generator(settings, word, pos, zh):
        raise AssertionError("generator should not run")

    service = SceneService(None, store=store, fetcher=fetcher, generator=generator)
    got = _run(service, "commit")
    assert got is not None and got.source == "tatoeba"
    assert store.get("commit") == got
    assert calls == ["commit"]


def test_generation_fallback(tmp_path: Path) -> None:
    store = ExampleStore(tmp_path)

    async def fetcher(word: str):
        return None

    async def generator(settings, word, pos, zh):
        return Example(
            word=word, en="We commit time to practice.", zh="我们投入时间练习。", source="generated"
        )

    service = SceneService(None, store=store, fetcher=fetcher, generator=generator)
    got = _run(service, "commit")
    assert got is not None and got.source == "generated"
    assert store.get("commit") == got


def test_none_when_both_fail(tmp_path: Path) -> None:
    async def fetcher(word: str):
        return None

    async def generator(settings, word, pos, zh):
        return None

    service = SceneService(None, store=ExampleStore(tmp_path), fetcher=fetcher, generator=generator)
    assert _run(service, "commit") is None


def test_pick_example_needs_bilingual_match() -> None:
    payload = {
        "results": [
            {"text": "Unrelated sentence.", "translations": [[{"lang": "cmn", "text": "无关。"}]]},
            {
                "text": "They commit to the plan.",
                "translations": [
                    [
                        {
                            "lang": "cmn",
                            "text": "他們投入這個計劃。",
                            "transcriptions": [{"script": "Hans", "text": "他们投入这个计划。"}],
                        }
                    ]
                ],
            },
        ]
    }
    got = pick_example("commit", payload)
    assert got is not None
    assert got.zh == "他们投入这个计划。"
    assert got.en == "They commit to the plan."


def test_pick_example_skips_unsafe() -> None:
    payload = {
        "results": [
            {
                "text": "He tried to commit suicide.",
                "translations": [[{"lang": "cmn", "text": "他试图自杀。"}]],
            },
            {
                "text": "They commit to the plan.",
                "translations": [[{"lang": "cmn", "text": "他们投入这个计划。"}]],
            },
        ]
    }
    got = pick_example("commit", payload)
    assert got is not None
    assert got.en == "They commit to the plan."


def test_two_senses_do_not_share_cache(tmp_path: Path) -> None:
    store = ExampleStore(tmp_path)
    fetch_calls = []

    async def fetcher(word: str):
        fetch_calls.append(word)
        return Example(
            word=word,
            en="They commit to the plan.",
            zh="他们投入这个计划。",
            source="tatoeba",
        )

    async def generator(settings, word, pos, zh):
        return Example(
            word=word,
            en="He committed a serious crime.",
            zh="他犯下了严重的罪行。",
            source="generated",
        )

    service = SceneService(None, store=store, fetcher=fetcher, generator=generator)
    first = _run(service, "commit", pos="v.", zh="投入")
    second = _run(service, "commit", pos="v.", zh="犯罪")

    assert first is not None and first.source == "tatoeba"
    assert second is not None and second.source == "generated"
    assert fetch_calls == ["commit", "commit"]
    assert store.get("commit", pos="v.", zh="投入") == first
    assert store.get("commit", pos="v.", zh="犯罪") == second


def test_sense_match_requires_chinese_gloss() -> None:
    from glyph.examples.service import sense_matches

    assert sense_matches("他们投入这个计划。", "投入；承诺")
    assert not sense_matches("他试图自杀。", "投入；承诺")
    assert sense_matches("任何句子。", "")
