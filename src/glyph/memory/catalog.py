"""目录页渲染：一行一个钩子，绝不半行截断。

- 每行 ≤ ``line_max_chars`` 个**字符**（不是字节），超了尾部 ``…``；
- 整篇 ≤ ``whole_max_bytes`` 个 UTF-8 **字节**，逐行累加预算，装不下的条目
  **整条丢弃**（不产出半个字 / 坏标记），最后补一行留痕告诉模型还有更多；
- 空本有一句说明，不返回空串。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .select import CatalogSelection
from .types import MemoryEntry

__all__ = ["clip_line", "render_catalog", "CatalogRenderCounts"]

_HEADER = (
    "# 长期记忆目录（每行只是一个钩子：需要细节时用 read_memory 按关键词读，不要凭这一行猜内容）"
)
_EMPTY = "（还没有记忆。用户说“记住：……”时会记在这里。）"


@dataclass(frozen=True)
class CatalogRenderCounts:
    dropped: int


def clip_line(line: str, max_chars: int) -> str:
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 1] + "…"


def render_catalog(
    entries: Sequence[MemoryEntry],
    *,
    line_max_chars: int,
    whole_max_bytes: int,
    selection: CatalogSelection | None = None,
) -> tuple[str, CatalogRenderCounts]:
    """渲染目录。``selection`` 可直接传入（injector 已先做过选择）。"""
    if selection is not None:
        dropped_before_render = selection.stale_count + selection.overflow_count
        chosen = selection.entries
    else:
        dropped_before_render = 0
        chosen = list(entries)

    # 候选行按展示顺序排好：头部 + 空行 + 条目行；统一逐行装预算，
    # 头部装不下也照样丢（极小预算场景），保证整篇绝不超 whole_max_bytes。
    candidates = [_HEADER, ""]
    candidates += [
        clip_line(
            f"- [{entry.type.value}] {entry.key} {entry.title}".rstrip(),
            line_max_chars,
        )
        for entry in chosen
    ]

    lines: list[str] = []
    used = 0
    dropped = 0

    def _append(line: str) -> bool:
        nonlocal used
        size = len((line + "\n").encode("utf-8"))
        # 条目行要给留痕行预留至少 60 字节；非条目行（头部）尽力装
        reservation = 60 if line.startswith("-") else 0
        if used + size + reservation > whole_max_bytes:
            return False
        lines.append(line)
        used += size
        return True

    for candidate in candidates:
        if candidate == "" or _append(candidate):
            if candidate == "":
                lines.append("")
            continue
        if candidate.startswith("-"):
            dropped += 1

    total_dropped = dropped_before_render + dropped
    if total_dropped > 0:
        ledger = (
            f"…（目录页装不下 / 有条目暂时退出，共省略 {total_dropped} 条："
            "可用 read_memory 按关键词读）"
        )
        clipped = clip_line(ledger, line_max_chars)
        if used + len((clipped + "\n").encode("utf-8")) <= whole_max_bytes:
            lines.append(clipped)

    if not chosen and dropped_before_render == 0:
        _append(_EMPTY)

    return "\n".join(lines).strip() + "\n", CatalogRenderCounts(dropped=total_dropped)
