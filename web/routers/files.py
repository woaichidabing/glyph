"""files 路由（独立模块，app.include_router 挂载）。"""

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()


def _decode_text(raw: bytes) -> str | None:
    """文本解码回退链：utf-8 → gbk → gb18030 → latin-1（latin-1 必成功）。"""
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _persist_upload(raw: bytes, ext: str) -> rout.Path:
    """把上传的文本文件落到上传目录，返回绝对路径（§6.1：供 read_file 消费）。"""
    state.upload_dir.mkdir(parents=True, exist_ok=True)
    path = state.upload_dir / f"{rout.uuid.uuid4().hex}{ext}"
    path.write_bytes(raw)
    return path


@router.post("/api/upload")
async def upload(file: rout.Annotated[rout.UploadFile, rout.File(...)]) -> dict:
    """接收上传文件，按 §6.1 分流：图片 → 内存仓（ocr 通道）；txt/md → 落盘
    （read_file 通道）；其余在服务端直接拒绝，不进工具、不占配额。"""
    raw = await file.read()
    size = len(raw)
    ext = rout.Path(file.filename or "").suffix.lower()

    if ext in rout._IMAGE_EXTS:
        mime = rout._IMAGE_MIME.get(ext) or file.content_type or "application/octet-stream"
        image_id = state.image_store.put(raw, mime)
        return {
            "filename": file.filename,
            "size": size,
            "content_type": file.content_type,
            "content": None,
            "is_text": False,
            "path": None,
            "ext": ext,
            "accepted": True,
            "channel": "ocr",
            "reason": None,
            "image_id": image_id,
        }

    if ext in rout._TEXT_EXTS:
        content = _decode_text(raw)
        path = _persist_upload(raw, ext)
        return {
            "filename": file.filename,
            "size": size,
            "content_type": file.content_type,
            "content": content,
            "is_text": content is not None,
            "path": str(path),
            "ext": ext,
            "accepted": True,
            "channel": "read_file",
            "reason": None,
            "image_id": None,
        }

    # gif 单独给可操作文案（addendum §6.1），不让它落到下面那条通用拒绝
    if ext == ".gif":
        return {
            "filename": file.filename,
            "size": size,
            "content_type": file.content_type,
            "content": None,
            "is_text": False,
            "path": None,
            "ext": ext,
            "accepted": False,
            "channel": None,
            "reason": "gif 请另存为 png / jpg 后上传",
            "image_id": None,
        }

    return {
        "filename": file.filename,
        "size": size,
        "content_type": file.content_type,
        "content": None,
        "is_text": False,
        "path": None,
        "ext": ext,
        "accepted": False,
        "channel": None,
        "reason": f"暂不支持 {ext or '无后缀'} 文件，请另存为 txt / md，或截图后按图片上传",
        "image_id": None,
    }

__all__ = [
    "_decode_text",
    "_persist_upload",
    "upload",
]
