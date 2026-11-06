"""Web 台兼容入口（阶段 1b：app / runtime / deps / routers 分层）。

`uvicorn web.server:app` 仍然可用；旧测试与旧调用方可以继续从 `web.server`
拿到路由函数、请求模型和少量服务别名。真正的实现在：

- web/app.py       创建 FastAPI、挂 lifespan、include 路由
- web/runtime.py   运行时装配、生命周期、公共函数
- web/deps.py      共享状态单点
- web/routers/     按功能拆分的路由
"""

from __future__ import annotations

# 兼容导出：这些符号被旧测试 / 旧调用方从 web.server 直接取用。
# ruff: noqa: F401
from fastapi import HTTPException

from glyph.config import load_settings
from glyph.provider import ProviderConfig, ProviderDiscoverError, load_provider_config
from glyph.session import SessionNotFoundError

from . import runtime
from .app import app
from .deps import state
from .routers import ask as _ask_routes
from .routers import config as _config_routes
from .routers import dictionary as _dictionary_routes
from .routers import files as _files_routes
from .routers import grade as _grade_routes
from .routers import plan as _plan_routes
from .routers import sessions as _sessions_routes
from .routers import storage as _storage_routes
from .routers import study as _study_routes
from .routers import wordbook as _wordbook_routes

_ROUTE_MODULES = (
    _sessions_routes,
    _ask_routes,
    _config_routes,
    _dictionary_routes,
    _files_routes,
    _grade_routes,
    _plan_routes,
    _storage_routes,
    _wordbook_routes,
    _study_routes,
)
for _routes in _ROUTE_MODULES:
    for _name in _routes.__all__:
        globals()[_name] = getattr(_routes, _name)

# 兼容旧调用方：路由函数 / 请求模型同上；以下是少数直接用的服务符号。
_QuotaGuardedRecheckTool = runtime._QuotaGuardedRecheckTool
_build_runtime = runtime._build_runtime
_make_loop = runtime._make_loop
_ensure_current_session = runtime._ensure_current_session
_switch_session = runtime._switch_session
