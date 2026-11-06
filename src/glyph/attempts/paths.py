"""尝试记录的存储根目录与文件路径单点（T07，对齐 memory/paths.py）。

- 默认根：``~/.markr/attempts``，与长期记忆 ``~/.markr/memory`` 平级；
- ``GLYPH_ATTEMPTS_ROOT`` 可覆盖根（测试与便携部署用）；
- 条目文件名只允许内容派生的 12 位十六进制 id，从路径层就堵死穿越。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from ..storage import data_root

__all__ = [
    "attempts_root",
    "attempt_path",
    "trash_dir",
    "trash_path",
    "is_valid_attempt_id",
]

_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def attempts_root() -> Path:
    """尝试记录根目录：环境变量优先，否则 ``~/.markr/attempts``。"""
    raw = os.environ.get("GLYPH_ATTEMPTS_ROOT")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return data_root() / "attempts"


def is_valid_attempt_id(raw: object) -> bool:
    """只认内容派生的 12 位小写十六进制；其它一律非法（防路径穿越）。"""
    return isinstance(raw, str) and bool(_ID_RE.match(raw))


def attempt_path(root: Path, attempt_id: str) -> Path:
    return Path(root) / f"attempt-{attempt_id}.md"


def trash_dir(root: Path) -> Path:
    return Path(root) / ".trash"


def trash_path(root: Path, attempt_id: str, now: datetime) -> Path:
    """软删目标：.trash 下原名追加时间戳，保证可追溯、不覆盖历史删除。"""
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    return trash_dir(root) / f"attempt-{attempt_id}-{stamp}.md"
