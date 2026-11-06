"""存储位置接口：查看、切换、打开、迁移。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from glyph.storage import (
    data_root,
    default_data_root,
    legacy_data_root,
    migrate_data,
    session_count,
    set_data_root,
    usage,
)

router = APIRouter()

__all__ = [
    "router",
    "get_storage",
    "change_root",
    "migrate",
    "open_folder",
    "ChangeRootRequest",
    "MigrateRequest",
]


class ChangeRootRequest(BaseModel):
    path: str = Field(min_length=1)
    migrate: bool = True


class MigrateRequest(BaseModel):
    source: str | None = None


def _info() -> dict:
    root = data_root()
    try:
        writable = root.exists() and os.access(root, os.W_OK)
    except OSError:
        writable = False
    return {
        "root": str(root),
        "default_root": str(default_data_root()),
        "legacy_root": str(legacy_data_root()),
        "writable": writable,
        "bytes": usage(root),
        "sessions": session_count(root),
    }


@router.get("/api/storage")
def get_storage() -> dict:
    return _info()


@router.post("/api/storage/root")
def change_root(req: ChangeRootRequest) -> dict:
    raw = req.path.strip()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="请选择绝对路径。")
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=400, detail="这个路径不是文件夹。")
    current = data_root()
    copied = 0
    if req.migrate and current.resolve() != target.resolve():
        try:
            copied = migrate_data(current, target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        set_data_root(target)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    info = _info()
    info["copied_files"] = copied
    return info


@router.post("/api/storage/migrate")
def migrate(req: MigrateRequest) -> dict:
    target = data_root()
    source = Path(req.source).expanduser() if req.source else legacy_data_root()
    try:
        copied = migrate_data(source, target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    info = _info()
    info["copied_files"] = copied
    return info


@router.post("/api/storage/open")
def open_folder() -> dict:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(root))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(root)])
        else:
            subprocess.Popen(["xdg-open", str(root)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"打开文件夹失败：{exc}") from exc
    return {"ok": True, "root": str(root)}
