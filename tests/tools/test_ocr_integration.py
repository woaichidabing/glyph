"""ocr_image 的真模型冒烟（marker=integration，默认不跑，守 R6）。

跑法（需 markr/.env 配好 key）：

    .venv\\Scripts\\python -m pytest -o addopts="" -m integration -q

真样张默认取 ``GLYPH_OCR_SAMPLE_IMAGE`` 指向的图片；未设置该环境变量就跳过
（仓库里不放图片，也不让测试依赖本机路径）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from glyph.config import load_settings
from glyph.tools import build_ocr_image_tool

pytestmark = pytest.mark.integration


class _FileProvider:
    """测试专用：直接按文件路径取字节（真实实现由装配层给出）。"""

    def __init__(self, path: Path, mime: str) -> None:
        self._path = path
        self._mime = mime

    async def read(self, image_id: str) -> tuple[bytes, str]:
        # 测试用：同步读文件即可（真实现由装配层给出，不受此规则约束）
        return self._path.read_bytes(), self._mime  # noqa: ASYNC240


async def test_真视觉模型转写手写作文() -> None:
    raw_path = os.environ.get("GLYPH_OCR_SAMPLE_IMAGE")
    if not raw_path:
        pytest.skip("未设置 GLYPH_OCR_SAMPLE_IMAGE，跳过真视觉冒烟")
    path = Path(raw_path)
    if not path.is_file():  # noqa: ASYNC240
        pytest.skip(f"样张不存在：{path}")

    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key")

    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/jpeg")

    tool = build_ocr_image_tool(_FileProvider(path, mime), settings)
    out = await tool.run({"image_id": "smoke"})

    # 职责闸：真模型路径同样不得出现任何批改结论
    assert "detections" not in out
    assert out["text"], "真模型返回了空转写，需要人工查看样张"
    # 转写结果应当以英文为主（降噪后不应残留纯中文界面行）
    assert any(c.isascii() and c.isalpha() for c in out["text"])

    print(f"\n[ocr smoke] model={settings.vision_model}")
    print(f"[ocr smoke] text={out['text']!r}")
    print(f"[ocr smoke] low_confidence={out['low_confidence']}")
    print(f"[ocr smoke] warnings={out['warnings']}")
