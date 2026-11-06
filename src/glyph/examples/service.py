"""例句服务：缓存 → Tatoeba → 模型生成。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from glyph.config import Settings

from .generator import generate_example
from .store import ExampleStore
from .tatoeba import fetch_example
from .types import Example

logger = logging.getLogger("glyph.examples")

Fetcher = Callable[[str], Awaitable[Example | None]]
Generator = Callable[[Settings | None, str, str, str], Awaitable[Example | None]]


def sense_matches(translation: str, gloss: str) -> bool:
    """中文例句至少命中当前义项里的一个词块；空义项不拦。"""
    if not gloss.strip():
        return True
    chunks = [x for x in re.split(r"[；;，,、/\s]+", gloss) if len(x.strip()) >= 2]
    return not chunks or any(x.strip() in translation for x in chunks)


def _sense_key(pos: str, zh: str) -> str:
    return "|".join(x.strip() for x in (pos, zh) if x.strip())


class SceneService:
    def __init__(
        self,
        settings: Settings | None,
        store: ExampleStore | None = None,
        fetcher: Fetcher | None = None,
        generator: Generator | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or ExampleStore()
        self.fetcher = fetcher or fetch_example
        self.generator = generator or generate_example
        self._missing: dict[str, float] = {}
        self._gate = asyncio.Semaphore(3)

    async def get(
        self,
        word: str,
        pos: str = "",
        zh: str = "",
        refresh: bool = False,
    ) -> Example | None:
        key = word.strip().lower()
        if not key:
            return None
        sense = _sense_key(pos, zh)
        miss_key = key + "|" + sense
        if not refresh:
            cached = self.store.get(key, pos=pos, zh=zh)
            if cached is not None:
                return cached
            missed_at = self._missing.get(miss_key, 0)
            if missed_at and time.time() - missed_at < 300:
                return None
        try:
            async with self._gate:
                found = await self.fetcher(key)
        except Exception as exc:  # noqa: BLE001 - 外部例句源失败不能杀学习卡
            logger.warning("Tatoeba 例句读取失败：%s", exc)
            found = None
        if found is not None and sense_matches(found.zh, zh):
            found = replace(found, sense=sense, pos=pos or found.pos)
            self.store.put(found)
            return found
        try:
            async with self._gate:
                made = await self.generator(self.settings, key, pos, zh)
        except Exception as exc:  # noqa: BLE001 - 生成失败退化为无场景
            logger.warning("例句生成失败：%s", exc)
            made = None
        if made is not None:
            made = replace(made, sense=sense, pos=pos or made.pos)
            self.store.put(made)
        else:
            self._missing[miss_key] = time.time()
        return made
