"""背词学习卡场景：例句库优先，模型兜底，本地缓存。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from glyph.examples import SceneService
from glyph.study import StudyStore

from ..deps import state

router = APIRouter()


def _study_store() -> StudyStore:
    if state.study_store is None:
        state.study_store = StudyStore()
    return state.study_store


@router.get("/api/study/state")
async def get_study_state() -> JSONResponse:
    return JSONResponse({"state": _study_store().get()})


@router.put("/api/study/state")
async def put_study_state(payload: dict) -> JSONResponse:
    _study_store().put(payload)
    return JSONResponse({"saved": True})


def _service() -> SceneService:
    if state.settings is None:
        raise HTTPException(status_code=503, detail="运行时还没准备好")
    if state.scene_service is None:
        state.scene_service = SceneService(state.settings)
    return state.scene_service


@router.delete("/api/study/scene")
async def forget_study_scene(word: str) -> JSONResponse:
    word = word.strip()
    if not word:
        raise HTTPException(status_code=400, detail="word 不能为空")
    _service().store.delete(word)
    return JSONResponse({"deleted": True})


@router.get("/api/study/scene")
async def study_scene(
    word: str,
    pos: str = "",
    zh: str = "",
    refresh: bool = False,
) -> JSONResponse:
    word = word.strip()
    if not word:
        raise HTTPException(status_code=400, detail="word 不能为空")
    example = await _service().get(word, pos=pos.strip(), zh=zh.strip(), refresh=refresh)
    return JSONResponse({"scene": example.to_dict() if example else None})


__all__ = ["router", "study_scene", "forget_study_scene", "get_study_state", "put_study_state"]
