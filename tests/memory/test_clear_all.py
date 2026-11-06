"""清空记忆：软删全部、条数正确、文件进回收站。"""

from __future__ import annotations

from glyph.memory.store import MemoryStore
from glyph.memory.types import MemoryEntry, MemoryType


def _entry(key: str) -> MemoryEntry:
    return MemoryEntry(
        key=key,
        type=MemoryType.PREFERENCE,
        title=key,
        quote="记一条测试内容。",
        count=1,
        first_seen="2026-11-01",
        last_seen="2026-11-01",
        last_mentioned_at="2026-11-01T00:00:00+08:00",
    )


def test_clear_all_soft_deletes_everything(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.upsert(_entry("aaaaaa000001"))
    store.upsert(_entry("bbbbbb000002"))
    assert len(store.list_entries()) == 2

    removed = store.clear_all()

    assert removed == 2
    assert store.list_entries() == []
    assert (tmp_path / ".trash").exists()


def test_clear_all_on_empty_is_zero(tmp_path):
    store = MemoryStore(root=tmp_path)
    assert store.clear_all() == 0


def test_clear_all_moves_pending_logs_to_trash(tmp_path):
    store = MemoryStore(root=tmp_path)
    store.upsert(_entry("cccccc000003"))
    logs = tmp_path / "logs" / "2026" / "11" / "05"
    logs.mkdir(parents=True)
    (logs / "s1-标题.md").write_text("暂存", encoding="utf-8")

    store.clear_all()

    assert not (tmp_path / "logs").exists()
    assert list((tmp_path / ".trash").rglob("s1-标题.md"))
