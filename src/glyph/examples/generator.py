"""模型兜底：只在例句库没有时写一句，校验后缓存。"""

from __future__ import annotations

import json
import re

from glyph.config import Settings

from .filters import is_safe_sentence
from .types import Example

SYSTEM = "你是一位英语例句写手。只输出 JSON，不要解释。"


def _prompt(word: str, pos: str, zh: str) -> str:
    return (
        "写一句自然、简单的英文例句，帮助中文学习者记住单词。\n"
        + "单词："
        + word
        + "\n"
        + "词性："
        + (pos or "不限")
        + "\n"
        + "中文义项："
        + (zh or "按常用义项")
        + "\n"
        + "要求：8 到 18 个英文词；只用这个义项；不要专有名词；不要引号；不要换行。\n"
        + '输出 JSON：{"en":"...","zh":"..."}'
    )


def _parse(raw: str) -> tuple[str, str] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    en = str(data.get("en", "")).strip()
    zh = str(data.get("zh", "")).strip()
    return (en, zh) if en and zh else None


async def generate_example(
    settings: Settings | None,
    word: str,
    pos: str,
    zh: str,
    *,
    client=None,
) -> Example | None:
    if settings is None or not getattr(settings, "api_key", ""):
        return None
    created = None
    if client is None:
        import openai

        created = openai.AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=30.0,
        )
        client = created
    try:
        res = await client.chat.completions.create(
            model=settings.compact_model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(word, pos, zh)},
            ],
            temperature=0.7,
            max_tokens=220,
            response_format={"type": "json_object"},
        )
        raw = res.choices[0].message.content or ""
        parsed = _parse(raw)
        if parsed is None:
            return None
        en, meaning = parsed
        if not re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", en, re.I):
            return None
        if not is_safe_sentence(en):
            return None
        if not (4 <= len(en.split()) <= 28):
            return None
        return Example(word=word.lower(), en=en, zh=meaning, source="generated", pos=pos)
    except Exception:
        return None
    finally:
        if created is not None:
            try:
                await created.close()
            except Exception:
                pass
