"""wordbook 路由（独立模块，app.include_router 挂载）。"""

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()

# ---------- 用户生词本 + 查词历史（localStorage 搬到后端） ----------
# 数据形状见 markr/wordbook/store.py：items 最多 200、history 最多 50，新的在前。


class WordbookAddIn(rout.BaseModel):
    entry: dict[str, rout.Any]


class WordbookRestoreIn(rout.BaseModel):
    item: dict[str, rout.Any]


class WordbookHistoryIn(rout.BaseModel):
    word: str


class WordbookImportIn(rout.BaseModel):
    items: list[dict[str, rout.Any]] = []
    history: list[dict[str, rout.Any]] = []


def _wordbook_bad_request(exc: ValueError) -> rout.HTTPException:
    """store 的词形 / 结构校验失败统一翻成 400（不是 404、也不是 500）。"""
    return rout.HTTPException(status_code=400, detail=str(exc) or "生词本参数不合法")


@router.get("/api/user/wordbook")
async def get_user_wordbook() -> dict:
    """整本生词本 + 查词历史（深拷贝快照）。"""
    return state.wordbook_store.snapshot()


@router.post("/api/user/wordbook")
async def add_user_wordbook(req: WordbookAddIn) -> dict:
    """收藏一个词典条目；同词幂等，保留第一次的收藏时间与释义快照。"""
    try:
        snapshot, item = state.wordbook_store.add(req.entry)
    except ValueError as exc:
        raise _wordbook_bad_request(exc) from exc
    return {**snapshot, "item": item}


@router.post("/api/user/wordbook/restore")
async def restore_user_wordbook(req: WordbookRestoreIn) -> dict:
    """撤销移出：按原 addedAt 插回原位；已存在则原样返回。"""
    try:
        snapshot, item = state.wordbook_store.restore(req.item)
    except ValueError as exc:
        raise _wordbook_bad_request(exc) from exc
    return {**snapshot, "item": item}


@router.post("/api/user/wordbook/history")
async def record_wordbook_lookup(req: WordbookHistoryIn) -> dict:
    """记一次查词：已有则 count+1、更新时间并移到最前。"""
    try:
        snapshot, item = state.wordbook_store.record_lookup(req.word)
    except ValueError as exc:
        raise _wordbook_bad_request(exc) from exc
    return {**snapshot, "item": item}


@router.post("/api/user/wordbook/import")
async def import_user_wordbook(req: WordbookImportIn) -> dict:
    """导入旧前端的 localStorage 快照；按 word 合并，重复导入幂等。"""
    try:
        snapshot, imported_items, imported_history = state.wordbook_store.import_state(
            req.items, req.history
        )
    except ValueError as exc:
        raise _wordbook_bad_request(exc) from exc
    return {
        **snapshot,
        "importedItems": imported_items,
        "importedHistory": imported_history,
    }


# 注意：/history 必须注册在 /{word} 之前，否则 DELETE /history 会被当成删一个叫
# history 的生词（FastAPI 按注册顺序匹配路径）。
@router.delete("/api/user/wordbook/history")
async def clear_wordbook_history() -> dict:
    """只清查词历史，生词本保留。"""
    return state.wordbook_store.clear_history()


@router.delete("/api/user/wordbook")
async def clear_user_wordbook() -> dict:
    """只清生词本，查词历史保留。"""
    return state.wordbook_store.clear_items()


@router.delete("/api/user/wordbook/{word}")
async def delete_user_wordbook_item(word: str) -> dict:
    """移出一个生词；词不存在时 removed=null（不 404）。"""
    try:
        snapshot, removed = state.wordbook_store.remove(word)
    except ValueError as exc:
        raise _wordbook_bad_request(exc) from exc
    return {**snapshot, "removed": removed}

__all__ = [
    "WordbookAddIn",
    "WordbookRestoreIn",
    "WordbookHistoryIn",
    "WordbookImportIn",
    "_wordbook_bad_request",
    "get_user_wordbook",
    "add_user_wordbook",
    "restore_user_wordbook",
    "record_wordbook_lookup",
    "import_user_wordbook",
    "clear_wordbook_history",
    "clear_user_wordbook",
    "delete_user_wordbook_item",
]
