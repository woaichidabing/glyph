"""dictionary 路由（独立模块，app.include_router 挂载）。"""

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()


class DictLookupRequest(rout.BaseModel):
    word: str


@router.post("/api/dict/lookup")
async def dict_lookup(req: DictLookupRequest) -> dict:
    """本地 ECDICT 查词：纯查词库、不走模型（更快、不花 token、不会编）。
    未收录返回 found=false + 空 senses，前端显示"未收录"，不报错。"""
    entry = state.dict_repo.lookup(req.word)
    if entry is None:
        return {"word": req.word.strip(), "found": False, "senses": []}
    entry["found"] = True
    return entry


# ---------- 词书：ECDICT 按考试标签出真实词表（背词页） ----------


@router.get("/api/wordbook/books")
async def wordbook_books() -> dict:
    """列出有词的考试词书及真实词量；词库 db 缺失时 503，前端降级样例词库。"""
    stats = state.dict_repo.book_stats()
    books = [b for b in stats if b["total"] > 0]
    if not books:
        raise rout.HTTPException(status_code=503, detail="词库未就绪")
    return {"books": books}


@router.get("/api/wordbook/words")
async def wordbook_words(tag: str, cursor: int = 0, limit: int = 600) -> dict:
    """取某本词书的一页词表（按词频高频在前）。只回单词原形，释义走查词缓存。"""
    tag = (tag or "").strip().lower()
    if tag not in rout.TAG_ORDER:
        raise rout.HTTPException(status_code=400, detail="未知词书")
    if not state.dict_repo.is_available():
        raise rout.HTTPException(status_code=503, detail="词库未就绪")
    return state.dict_repo.book_words(tag, max(0, cursor), max(1, min(limit, 2000)))

__all__ = [
    "DictLookupRequest",
    "dict_lookup",
    "wordbook_books",
    "wordbook_words",
]
