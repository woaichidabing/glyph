"""web/ 不在 src 布局内：把项目根加进 sys.path，让 ``import web.server`` 可用。

web/server.py 自己也是同款姿势注入 src（R9 不做 editable 安装）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.server as srv
from glyph.runtime.registry import ToolRegistry
from web.deps import state as web_state

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture()
def web_env(monkeypatch, tmp_path):
    """把 web.deps.state 摆成可测状态：假 llm/loop、固定会话、临时上传目录。"""
    # 供应商配置文件也必须隔离：v2 起 /api/models 与 /api/providers 都从这份文件读，
    # 不隔离就会读到开发机真实的 ~/.markr/provider.json（测试结果随人而异，最坏是假绿）。
    monkeypatch.setenv("GLYPH_PROVIDER_CONFIG", str(tmp_path / "provider.json"))
    # 凭据文件同样隔离：v2 起钥匙只存在 ~/.markr/credentials.json（配置里只放名字），
    # 不隔离会读到开发机的真凭据，用例之间还会互相污染（前一个用例存过的钥匙被后一个读到）。
    monkeypatch.setenv("GLYPH_CREDENTIALS", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(web_state, "settings", srv.load_settings())
    monkeypatch.setattr(web_state, "llm", object())
    monkeypatch.setattr(web_state, "registry", ToolRegistry())
    monkeypatch.setattr(web_state, "loop", SimpleNamespace(busy=False))
    monkeypatch.setattr(web_state, "history", SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(web_state, "session_id", "test-sid")
    monkeypatch.setattr(web_state, "upload_dir", tmp_path)
    monkeypatch.setattr(web_state, "memory_injector", SimpleNamespace(fragment=lambda: None))
    # 会话池是模块级状态：不清理会让上一个用例的假会话漏到下一个用例里
    web_state.sessions.clear()
    web_state.recheck_ledger.clear()
    yield
    web_state.sessions.clear()
    web_state.recheck_ledger.clear()
