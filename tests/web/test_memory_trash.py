"""记忆回收站：统计、手动清理、30 天自动清理。"""
from pathlib import Path

from fastapi.testclient import TestClient

from glyph.memory.paths import memory_root
from glyph.memory.store import MemoryStore
from web.app import app


def _make_trash(tmp_path, monkeypatch, days_old: int = 0):
    monkeypatch.setenv("GLYPH_DATA_ROOT", str(tmp_path))
    trash = memory_root() / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    f = trash / "mistake-abc-20260101-000000.md"
    f.write_text("x" * 100, encoding="utf-8")
    if days_old:
        import os
        import time
        past = time.time() - days_old * 86400
        os.utime(f, (past, past))
    return f


def test_trash_stats_and_purge(tmp_path, monkeypatch):
    _make_trash(tmp_path, monkeypatch)
    client = TestClient(app)
    stats = client.get("/api/memory/trash").json()
    assert stats["count"] == 1
    assert stats["bytes"] == 100
    out = client.delete("/api/memory/trash").json()
    assert out["removed"] == 1
    assert client.get("/api/memory/trash").json()["count"] == 0


def test_purge_keeps_recent_when_older_than(tmp_path, monkeypatch):
    f_old = _make_trash(tmp_path, monkeypatch, days_old=40)
    f_new = memory_root() / ".trash" / "preference-def-20260201-000000.md"
    f_new.write_text("y" * 20, encoding="utf-8")
    removed, freed = MemoryStore(root=memory_root()).purge_trash(older_than_days=30)
    assert removed == 1 and freed == 100
    assert not f_old.exists() and f_new.exists()
