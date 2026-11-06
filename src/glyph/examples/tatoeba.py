"""Tatoeba 英中句对：只取公开、带中文翻译的句子。"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request

from .filters import is_safe_sentence
from .types import Example

API = "https://tatoeba.org/en/api_v0/search"


def _boundary(word: str, text: str) -> bool:
    if not word or not text:
        return False
    return re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", text, re.I) is not None


def _zh_text(translation: dict) -> str:
    for item in translation.get("transcriptions", []) or []:
        if isinstance(item, dict) and item.get("script") == "Hans" and item.get("text"):
            return str(item["text"]).strip()
    return str(translation.get("text", "")).strip()


def pick_example(word: str, payload: dict) -> Example | None:
    """从 API 返回里挑一条短、带中文、目标词真的出现的句子。"""
    if not isinstance(payload, dict):
        return None
    for row in payload.get("results", []) or []:
        if not isinstance(row, dict):
            continue
        en = str(row.get("text", "")).strip()
        if not en or "\n" in en or not _boundary(word, en):
            continue
        if not is_safe_sentence(en):
            continue
        if not (4 <= len(en.split()) <= 24):
            continue
        for group in row.get("translations", []) or []:
            for tr in group if isinstance(group, list) else []:
                if not isinstance(tr, dict) or tr.get("lang") != "cmn":
                    continue
                zh = _zh_text(tr)
                if zh:
                    return Example(word=word.lower(), en=en, zh=zh, source="tatoeba")
    return None


def _fetch_sync(word: str, timeout: float) -> Example | None:
    query = urllib.parse.urlencode({"from": "eng", "to": "cmn", "query": word})
    req = urllib.request.Request(
        API + "?" + query,
        headers={"User-Agent": "Glyph/1.0 (local learning app)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return pick_example(word, json.loads(body))


async def fetch_example(word: str, wait: float = 6.0) -> Example | None:
    try:
        return await asyncio.to_thread(_fetch_sync, word, wait)
    except Exception:
        return None
