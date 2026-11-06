"""看图工具的公共底座：图片从哪来、什么算图片、多大算大。

现在有两个工具都要读同一张图：

* ``ocr_image`` —— 转写图里的英文，为了拿原文去批改；
* ``describe_image`` —— 看懂图里有什么，为了回答用户关于这张图的问题。

两处各写一份「什么格式算图片、多大算大」必然漂移（同一个用户传同一张图，
一个能看一个不能，最难查）。收在这里，口径只有一份。
"""

from __future__ import annotations

from typing import Protocol

from ..runtime.errors import ToolError

# 只允许常见位图格式；其余（含 pdf / 未知类型）一律拒绝，不把二进制硬塞给模型
MIME_WHITELIST = frozenset({"image/jpeg", "image/png", "image/webp", "image/bmp"})

__all__ = ["ImageProvider", "MIME_WHITELIST", "check_image"]


class ImageProvider(Protocol):
    """按图片编号取回字节。由装配层（web / 桌面端）实现，工具不认识文件系统。"""

    async def read(self, image_id: str) -> tuple[bytes, str]:
        """返回 ``(字节, mime)``；找不到或读取失败时抛异常。"""


def check_image(image_bytes: bytes, mime: str, max_bytes: int) -> None:
    """入参校验单点（R5）：格式白名单 → 体积上限 → 空图，不合格一律 ``ToolError``。

    放在这里而不是两个工具里各写一遍，是因为「哪一处漏了体积检查」这种差异
    只能靠肉眼审；收成一处后只有一份代码要维护。
    """
    if mime not in MIME_WHITELIST:
        raise ToolError(f"不支持的图片格式（{mime}），请提供 jpg / png / webp / bmp 图片")
    if len(image_bytes) > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        raise ToolError(
            f"图片过大（{len(image_bytes) / (1024 * 1024):.1f}MB，上限 {limit_mb:.0f}MB），"
            "请压缩或裁剪后再上传"
        )
    if not image_bytes:
        raise ToolError("图片内容为空，无法处理")
