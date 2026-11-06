"""背词状态路径。"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import data_root


def study_root() -> Path:
    raw = os.environ.get("GLYPH_STUDY_ROOT", "").strip()
    return Path(raw).expanduser() if raw else data_root() / "study"


def state_path(root: Path) -> Path:
    return root / "state.json"
