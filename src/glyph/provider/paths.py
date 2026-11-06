"""模型供应商配置的落盘位置（R1 路径单点，纯函数不读盘）。

沿用 sessions / memory / attempts 的 paths.py 模式：
- 默认文件：``~/.markr/provider.json``（与各子系统目录平级的一个文件）；
- ``GLYPH_PROVIDER_CONFIG`` 可直接指定文件路径（测试注入 tmp_path、便携部署用）。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import data_root

__all__ = ["PROVIDER_CONFIG_ENV", "provider_config_path"]

PROVIDER_CONFIG_ENV = "GLYPH_PROVIDER_CONFIG"
"""供应商配置文件路径的环境变量名，全项目只有 ``provider_config_path()`` 读它。"""


def provider_config_path() -> Path:
    """供应商配置文件：环境变量优先，否则 ``~/.markr/provider.json``。"""
    raw = os.environ.get(PROVIDER_CONFIG_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_root() / "provider.json"
