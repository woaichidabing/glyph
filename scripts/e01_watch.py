"""E01 监听器命令入口。

运行：
    .venv\\Scripts\\python scripts\\e01_watch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from glyph.e01_watch import main  # noqa: E402, I001


if __name__ == "__main__":
    raise SystemExit(main())
