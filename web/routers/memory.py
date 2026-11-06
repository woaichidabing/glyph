"""记忆的查看、清空与回收站（设置页用）。

清空是软删：文件进 .trash，可手动找回；不碰会话、词书、计划。
回收站可以永久清理；启动时另有一道 30 天自动清理（见 runtime.lifespan）。
"""

from __future__ import annotations

from fastapi import APIRouter

from glyph.memory.paths import memory_root
from glyph.memory.store import MemoryStore

router = APIRouter()


def _store() -> MemoryStore:
    return MemoryStore(root=memory_root())


@router.get("/api/memory")
def get_memory() -> dict:
    entries = _store().list_entries()
    return {"count": len(entries)}


@router.post("/api/memory/clear")
def clear_memory() -> dict:
    removed = _store().clear_all()
    return {"removed": removed}


@router.get("/api/memory/trash")
def get_memory_trash() -> dict:
    count, size = _store().trash_stats()
    return {"count": count, "bytes": size}


@router.delete("/api/memory/trash")
def purge_memory_trash() -> dict:
    removed, freed = _store().purge_trash()
    return {"removed": removed, "freed": freed}
