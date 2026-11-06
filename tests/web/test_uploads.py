"""上传分流接口测试（从 test_server.py 拆出）。"""

from __future__ import annotations

import io
from pathlib import Path

from fastapi import UploadFile
from starlette.datastructures import Headers

import web.server as srv
from web.deps import state as web_state

# ---------- 帮手 ----------


def _upload(name: str, data: bytes, content_type: str) -> UploadFile:
    """构造一个可直接传给 upload handler 的 UploadFile（不走 HTTP 层）。"""
    return UploadFile(
        file=io.BytesIO(data),
        size=len(data),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


# ---------- /api/upload：§6.1 分流（图片 → image_id 通道；txt/md → read_file；其余拒绝） ----------


async def test_upload_image_goes_to_ocr_channel(web_env):
    resp = await srv.upload(file=_upload("essay.jpg", b"\xff\xd8fakejpg", "image/jpeg"))
    assert resp["accepted"] is True
    assert resp["channel"] == "ocr"
    assert resp["image_id"]
    assert resp["is_text"] is False
    assert resp["content"] is None
    # 图片字节进内存仓，ocr_image 工具按 image_id 能取回
    data, mime = await web_state.image_store.read(resp["image_id"])
    assert data == b"\xff\xd8fakejpg"
    assert mime == "image/jpeg"


async def test_upload_image_mime_inferred_from_ext(web_env):
    """浏览器偶尔给 octet-stream：mime 按扩展名推断，不能只信 content_type。"""
    resp = await srv.upload(file=_upload("shot.png", b"pngbytes", "application/octet-stream"))
    assert resp["channel"] == "ocr"
    _, mime = await web_state.image_store.read(resp["image_id"])
    assert mime == "image/png"


async def test_upload_txt_persists_and_returns_path(web_env):
    resp = await srv.upload(file=_upload("essay.txt", b"Hello world", "text/plain"))
    assert resp["accepted"] is True
    assert resp["channel"] == "read_file"
    assert resp["is_text"] is True
    assert resp["content"] == "Hello world"
    path = Path(resp["path"])
    # 测试线程同步读 tmp_path，无事件循环阻塞风险，豁免 ASYNC240
    assert path.exists()  # noqa: ASYNC240
    assert path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "Hello world"  # noqa: ASYNC240


async def test_upload_md_goes_to_read_file_channel(web_env):
    resp = await srv.upload(file=_upload("notes.md", b"# Title", "text/markdown"))
    assert resp["accepted"] is True
    assert resp["channel"] == "read_file"


async def test_upload_gbk_txt_still_decodes(web_env):
    """旧版编码回退链保留：GBK 文本也要能读出内容。"""
    resp = await srv.upload(file=_upload("old.txt", "你好".encode("gbk"), "text/plain"))
    assert resp["content"] == "你好"
    assert resp["accepted"] is True


async def test_upload_rejects_unsupported_ext(web_env):
    resp = await srv.upload(file=_upload("doc.docx", b"PK\x03\x04", "application/octet-stream"))
    assert resp["accepted"] is False
    assert resp["channel"] is None
    assert resp["reason"]
    assert resp["content"] is None
    assert resp["is_text"] is False
    assert resp["path"] is None
    assert resp.get("image_id") is None


async def test_upload_gif_rejected_with_actionable_reason(web_env):
    """gif 刻意不在图片白名单（addendum §6.1）：入口就拒并给可操作文案，
    不让用户上传成功、等到调 ocr_image 才被 MIME_WHITELIST 卡住。"""
    resp = await srv.upload(file=_upload("meme.gif", b"GIF89a", "image/gif"))
    assert resp["accepted"] is False
    assert resp["channel"] is None
    assert resp["reason"] == "gif 请另存为 png / jpg 后上传"
    assert resp.get("image_id") is None
