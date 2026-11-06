"""归一化与内容寻址去重键。

键里只放"身份字段"：mistake 用「原文片段 + 改法」，其余三类用 title ——
讲解、why 这类会改写的内容不进键，改了讲解不会变成新记忆。

mistake 的三级退化（2026-09-27 修，见 docs/review/memory-system.md）：
1. 给了原文 / 改法 → 用它俩（此时讲解怎么改都算同一条）；
2. 只给了其中一个 → 用给的这个（绝不拿空串凑身份，否则同一条错会落两个文件）；
3. 都没给 → 退化用整条正文（content，即落盘后的 quote）。

第 3 级是刻意的取舍：拿不到结构化身份时，**宁可多出近似重复，也不覆盖丢失**
（退化前的 bug 正是所有错题算成同一个 key、后一条覆盖前一条）。
代价是此时"改一个字就是新条目"，去重语义弱于前两级。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .types import MemoryType

__all__ = ["normalize_fragment", "compute_key"]

_WHITESPACE = re.compile(r"\s+")
_EDGE_PUNCT = ",.;:!?\"'"


def normalize_fragment(text: str) -> str:
    """NFKC → 小写 → 折叠所有空白为一个空格 → 去首尾空白与首尾标点。"""
    normalized = unicodedata.normalize("NFKC", text).lower()
    collapsed = _WHITESPACE.sub(" ", normalized).strip()
    return collapsed.strip(_EDGE_PUNCT).strip()


def compute_key(
    type_: MemoryType,
    *,
    title: str = "",
    original: str | None = None,
    correction: str | None = None,
    content: str = "",
) -> str:
    """内容寻址身份，12 位十六进制。见模块 docstring 的三级退化规则。

    ``content`` 即调用方要存的那句原话（落盘后是 ``MemoryEntry.quote``）。
    """
    if type_ is MemoryType.MISTAKE:
        original = (original or "").strip()
        correction = (correction or "").strip()
        if original and correction:
            norm_original = normalize_fragment(original)
            norm_correction = normalize_fragment(correction)
            identity = f"{type_.value}|{norm_original}|{norm_correction}"
        elif original or correction:
            # 只给了一半：用给的那一半，绝不拿空串凑身份
            identity = f"{type_.value}|{normalize_fragment(original or correction)}"
        else:
            identity = f"{type_.value}|{normalize_fragment(content or title)}"
    else:
        identity = f"{type_.value}|{normalize_fragment(title)}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
