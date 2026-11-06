"""打包入口：Glyph 本地后端（PyInstaller 打成一个 exe）。

用法: glyph-backend.exe --port 12345
- 端口由桌面端（Electron）分配；
- 前端静态文件位置由环境变量 GLYPH_WEB_DIST 指定；
- 用户数据目录沿用 storage.data_root()（老用户 ~/.markr，新装 ~/Documents/Glyph）。
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args, _ = parser.parse_known_args()
    port = args.port or int(os.environ.get("GLYPH_PORT", "0") or 0)
    if not port:
        print("缺少 --port", file=sys.stderr)
        raise SystemExit(2)

    import uvicorn

    from web.server import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
