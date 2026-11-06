"""真机冒烟：在真实磁盘上验「两条不同错题 = 两个文件」+「坏文件不炸」。

只写临时目录，不碰用户真数据。
用法：.venv/Scripts/python.exe scripts/verify_memory_real.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from glyph.memory.catalog import render_catalog  # noqa: E402
from glyph.memory.store import MemoryStore  # noqa: E402
from glyph.memory.types import MemoryEntry, MemoryType  # noqa: E402
from glyph.tools.memory_tools import compute_key  # noqa: E402


def _mistake(original: str, correction: str, title: str) -> MemoryEntry:
    """按 remember 工具的真实算键路径造一条错题。"""
    key = compute_key(MemoryType.MISTAKE, title=title, original=original, correction=correction)
    return MemoryEntry(
        key=key,
        type=MemoryType.MISTAKE,
        title=title,
        quote=f"{original} → {correction}",
        original=original,
        correction=correction,
        count=1,
        first_seen="2026-09-27",
        last_seen="2026-09-27",
        last_mentioned_at="2026-09-27",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = MemoryStore(root)

        # ① 两条真不一样的错题 → 必须是两个文件（旧实现恒等键，第二个会覆盖第一个）
        store.upsert(_mistake("He go to school", "He goes to school", "He go to school"))
        store.upsert(_mistake("I has a apple", "I have an apple", "I has a apple"))

        entries = store.list_entries()
        files = sorted(p.name for p in root.glob("*.md"))
        print(f"① 两条不同错题 → 条目数={len(entries)}")
        for f in files:
            print(f"   文件：{f}")
        for e in entries:
            print(f"   key={e.key}  {e.original!r} → {e.correction!r}")
        assert len(entries) == 2, "两条不同错题只剩一条 —— 恒等键问题没修掉"

        # ② 同一条错再记一次 → 计数 +1，不新增文件
        before = len(list(root.glob("*.md")))
        store.upsert(_mistake("He go to school", "He goes to school", "He go to school"))
        after = len(list(root.glob("*.md")))
        repeat = [e for e in store.list_entries() if e.original == "He go to school"]
        print(f"② 重复记同一条 → 文件数 {before}→{after}，count={repeat[0].count}")
        assert after == before, "重复记同一条错题不该多出文件"
        assert repeat[0].count == 2, f"count 应为 2，实际 {repeat[0].count}"

        # ③ 真实坏文件（GBK 编码、utf-8 解不开）→ 跳过 + 计数，不炸
        bad = root / "preference-badbad000001.md"
        bad.write_bytes("标题：坏文件\n正文：这一段是 GBK 字节，utf-8 解不开。".encode("gbk"))
        entries2 = store.list_entries()
        print(
            f"③ 混进一个解不开的文件 → 仍能列出 {len(entries2)} 条，"
            f"skipped_files={store.skipped_files}"
        )
        assert store.skipped_files == 1, "坏文件没被计数（要么没跳过，要么没数上）"

        # ④ 目录页渲染：坏文件不该让注入上下文这一步挂掉
        text, counts = render_catalog(
            entries2, line_max_chars=120, whole_max_bytes=32 * 1024
        )
        print(f"④ 目录页渲染成功：{len(text)} 字符，首行={text.splitlines()[0]!r}")

    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
