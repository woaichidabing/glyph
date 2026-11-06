"""例句安全过滤：学习卡不出现自残 / 性 / 暴力 / 毒品等不适合的句子。"""

from __future__ import annotations

import re

BANNED = {
    "suicide",
    "suicidal",
    "kill",
    "killed",
    "killing",
    "murder",
    "murdered",
    "rape",
    "raped",
    "sex",
    "sexual",
    "porn",
    "drug",
    "drugs",
    "cocaine",
    "heroin",
    "gun",
    "guns",
    "weapon",
    "weapons",
    "dead",
    "death",
    "die",
    "died",
    "blood",
    "violence",
    "violent",
    "abuse",
    "abused",
    "slave",
    "slavery",
    "nazi",
    "terrorist",
    "terrorism",
    "bomb",
    "bombing",
    "corpse",
}


def is_safe_sentence(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return not (words & BANNED)
