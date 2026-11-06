"""FastAPI 应用装配（阶段 1b）。

只做三件事：创建 app、挂 lifespan、按顺序 include 路由；业务在 runtime / routers。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import runtime
from .routers import (
    ask,
    memory,
    config,
    dictionary,
    files,
    grade,
    plan,
    prefs,
    sessions,
    storage,
    study,
    wordbook,
)

app = FastAPI(title="Glyph Test Web", lifespan=runtime.lifespan)

# 顺序与拆分前一致；静态挂载必须最后。
for _router_module in (
    sessions,
    ask,
    config,
    memory,
    dictionary,
    files,
    grade,
    plan,
    storage,
    wordbook,
    study,
):
    app.include_router(_router_module.router)
app.include_router(prefs.router)

# 桌面端：设置 GLYPH_WEB_DIST 指向打包好的前端目录，就用它当站点根；
# 没设置时仍是测试台的静态页（保持旧行为）。
_DIST = os.environ.get("GLYPH_WEB_DIST", "").strip()
_STATIC = Path(_DIST).expanduser() if _DIST else Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
else:
    # 桌面端由 Electron 传 GLYPH_WEB_DIST；万一没传，也别让整个后端起不来
    print("web: 静态目录不存在，跳过挂载: " + str(_STATIC))
