"""目录页渲染测试：行 150 字符、整篇 32 KiB、逐行不半切。"""

from glyph.memory.catalog import render_catalog
from glyph.memory.types import MemoryEntry, MemoryType


def entry(key: str, title: str, type_: MemoryType = MemoryType.PREFERENCE) -> MemoryEntry:
    return MemoryEntry(
        key=key,
        type=type_,
        title=title,
        quote="q",
        count=1,
        first_seen="2026-09-01",
        last_seen="2026-09-20",
        last_mentioned_at="2026-09-20",
    )


def test_render_lists_entry() -> None:
    text, counts = render_catalog(
        [entry("abcdef012345", "讲解用中文")],
        line_max_chars=150,
        whole_max_bytes=32 * 1024,
    )

    assert "讲解用中文" in text
    assert "abcdef012345" in text
    assert counts.dropped == 0


def test_long_line_is_clipped_not_broken() -> None:
    long_title = "字" * 200
    text, counts = render_catalog(
        [entry("abcdef012345", long_title)],
        line_max_chars=150,
        whole_max_bytes=32 * 1024,
    )

    lines = [line for line in text.split("\n") if line.startswith("-")]
    assert len(lines) == 1
    assert len(lines[0]) <= 150
    assert lines[0].endswith("…")


def test_whole_budget_drops_whole_lines_with_ledger() -> None:
    # 每条目行约 30+ 字节（中文）；预算只够放第一条，其余整条丢弃并留痕
    entries = [entry(f"key{i:08d}", f"标题{i}" + "内" * 20) for i in range(5)]
    text, counts = render_catalog(
        entries,
        line_max_chars=150,
        whole_max_bytes=120,
    )

    assert counts.dropped >= 1
    assert "省略" in text
    # 整篇仍在预算内
    assert len(text.encode("utf-8")) <= 120


def test_empty_catalog_has_friendly_text() -> None:
    text, counts = render_catalog([], line_max_chars=150, whole_max_bytes=32 * 1024)

    assert text.strip() != ""
    assert counts.dropped == 0
