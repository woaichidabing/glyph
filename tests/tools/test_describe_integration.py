"""describe_image 的真模型冒烟（marker=integration，默认不跑，守 R6）。

跑法（需 markr/.env 配好 key）：

    set GLYPH_DESCRIBE_SAMPLE_IMAGE=<图片绝对路径>
    .venv\\Scripts\\python -m pytest -o addopts="" -m integration -q

为什么要真跑：这个工具的**全部价值**就是"主模型看不见图、它看得见"。
离线测试只能证明接线对，证明不了它真能看懂 —— 而主模型 deepseek-v4-pro
面对看不见的图会编出一段像模像样的话，如果 describe_image 也编，
那我们等于加了一个"编得更自信"的入口，比不加更糟。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from glyph.config import load_settings
from glyph.tools import build_describe_image_tool

pytestmark = pytest.mark.integration


class _FileProvider:
    """测试专用：直接按文件路径取字节（真实实现由装配层给出）。"""

    def __init__(self, path: Path, mime: str) -> None:
        self._path = path
        self._mime = mime

    async def read(self, image_id: str) -> tuple[bytes, str]:
        # 测试用：同步读文件即可（真实现由装配层给出，不受此规则约束）
        return self._path.read_bytes(), self._mime  # noqa: ASYNC240


async def test_真视觉模型能看懂一张图() -> None:
    raw_path = os.environ.get("GLYPH_DESCRIBE_SAMPLE_IMAGE")
    if not raw_path:
        pytest.skip("未设置 GLYPH_DESCRIBE_SAMPLE_IMAGE，跳过真视觉冒烟")
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

    tool = build_describe_image_tool(_FileProvider(path, mime), settings)
    out = await tool.run({"image_id": "smoke"})

    # 职责闸：真模型路径同样不得出现任何批改结论
    assert "detections" not in out
    assert "summary" not in out
    assert out["description"], "真模型返回了空描述，需要人工查看样张"
    # 契约说好用简体中文回答：至少得有中文字符，不能回一段英文当描述
    assert any("一" <= ch <= "鿿" for ch in out["description"])

    print(f"\n[describe smoke] model={settings.vision_model}")
    print(f"[describe smoke] image={path.name}")
    print(f"[describe smoke] description={out['description']!r}")
    print(f"[describe smoke] warnings={out['warnings']}")
