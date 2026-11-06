"""例句缓存路径。"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import data_root


def example_root() -> Path:
    raw = os.environ.get("GLYPH_EXAMPLE_ROOT", "").strip()
    return Path(raw).expanduser() if raw else data_root() / "examples"


def state_path(root: Path) -> Path:
    return root / "examples.json"
