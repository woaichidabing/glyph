"""控制字符清洗 + 按 UTF-8 字节但不切坏多字节字符的安全截断。"""

from __future__ import annotations

import re

__all__ = ["sanitize_text", "clip_bytes"]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sanitize_text(raw: str) -> str:
    """归一换行 → 去 ANSI → 删 C0（除 \\n\\t）/ DEL / C1 控制符。"""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI.sub("", text)
    return "".join(
        ch
        for ch in text
        if ch in "\n\t" or not (ord(ch) < 0x20 or ord(ch) == 0x7F or 0x80 <= ord(ch) <= 0x9F)
    )


def clip_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """按字符累加 UTF-8 字节数截断，绝不切出半个字符。

    返回 (截断后的文本, 是否发生截断)。
    """
    kept: list[str] = []
    used = 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > max_bytes:
            return "".join(kept), True
        kept.append(ch)
        used += size
    return text, False
