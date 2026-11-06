"""计划数据落盘路径单点（R1）。"""

from __future__ import annotations

import os
from pathlib import Path

from ..storage import data_root

GLYPH_PLAN_ROOT_ENV = "GLYPH_PLAN_ROOT"
STATE_FILENAME = "state.json"

__all__ = ["GLYPH_PLAN_ROOT_ENV", "STATE_FILENAME", "plan_root", "state_path"]


def plan_root() -> Path:
    raw = os.environ.get(GLYPH_PLAN_ROOT_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_root() / "plan"


def state_path(root: str | Path) -> Path:
    return Path(root) / STATE_FILENAME
