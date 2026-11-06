"""偏好设置路由：外观等跨重启要记住的东西。

为什么放后端：桌面端每次启动端口都可能变，浏览器本地存储是按"网址"隔离的，
换个端口就等于换了个新网站，设置会读不到。存进数据目录才跨重启有效。
"""
import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_ALLOWED = {"appearance", "lastSession", "onboardingSkipped"}


def _prefs_path() -> Path:
    from glyph.storage import data_root
    return Path(data_root()) / "prefs.json"


def _read() -> dict:
    p = _prefs_path()
    try:
        raw = json.loads(p.read_text("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


class PrefsIn(BaseModel):
    key: str
    value: object


@router.get("/api/prefs")
async def get_prefs() -> dict:
    return _read()


@router.put("/api/prefs")
async def put_pref(body: PrefsIn) -> dict:
    if body.key not in _ALLOWED:
        raise HTTPException(status_code=400, detail="不认识的设置项")
    data = _read()
    data[body.key] = body.value
    p = _prefs_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        raise HTTPException(status_code=500, detail="设置没保存上") from e
    return {"ok": True}
