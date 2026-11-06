"""例句记录。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Example:
    word: str
    en: str
    zh: str
    source: str  # tatoeba | generated
    pos: str = ""
    sense: str = ""

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "en": self.en,
            "zh": self.zh,
            "source": self.source,
            "pos": self.pos,
            "sense": self.sense,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Example:
        return cls(
            word=str(raw.get("word", "")),
            en=str(raw.get("en", "")),
            zh=str(raw.get("zh", "")),
            source=str(raw.get("source", "")),
            pos=str(raw.get("pos", "")),
            sense=str(raw.get("sense", "")),
        )
