"""模型子系统的「用户旅程」端到端（离线，不起 HTTP 服务）。

这条用例走的是用户真实会做的顺序：
1. 加一档自定义供应商（地址 + 密钥 + 两个模型 + 思考档）→ 2. 启用它 →
3. 主界面模型入口列的就是它那两个 → 4. 切模型 → 5. 切思考档 →
6. 删掉它 → 主界面自动回落到还能用的那一档。

守的是"分步都做过、串起来却断"的那类问题：接口各自有测试，串起来没人测过。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import web.server as srv
from web import runtime as web_runtime

# tests/web 没有 __init__.py：按模块名导入同目录的脚手架
try:
    from test_providers_api import _custom_entry, _stub_rebuild
except ImportError:  # 直接跑单文件时的兜底
    from tests.web.test_providers_api import _custom_entry, _stub_rebuild


async def test_full_model_journey(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    rev = (await srv.get_providers())["revision"]

    # ① 加一档（带思考档：low/medium/high）
    entry = _custom_entry(
        revision=rev,
        models=[
            {"id": "deepseek-v4.1-flash-1017", "label": "V4.1 Flash", "context_window": 131072,
             "max_output": 8192, "inputs": ["text"], "reasoning_levels": ["low", "medium", "high"]},
            {"id": "qwen-plus", "label": "Qwen Plus", "context_window": 131072,
             "max_output": 8192, "inputs": ["text"], "reasoning_levels": []},
        ],
    )
    await srv.set_provider(entry)

    # ② 启用它（第一档自定义会自动成为当前档，这里显式再切一次确保幂等）
    rev = (await srv.get_providers())["revision"]
    active_id = (await srv.get_providers())["active"]
    await srv.activate_provider(active_id, revision=rev)

    # ③ 主界面入口 = 这一档的两个模型，且不显示供应商名
    listed = await srv.list_models()
    assert [m["id"] for m in listed["models"]] == ["deepseek-v4.1-flash-1017", "qwen-plus"]
    assert listed["default"] in {"deepseek-v4.1-flash-1017", "qwen-plus"}
    assert "provider" not in listed

    # ④ 切模型（带思考档）——这一步以前会因为"模型不支持该档"被 400 打回
    cfg = await srv.set_config(srv.ConfigRequest(model="deepseek-v4.1-flash-1017", reasoning_effort="high"))
    assert cfg["model"] == "deepseek-v4.1-flash-1017"
    assert cfg["reasoning_effort"] == "high"

    # ⑤ 档位落盘：重启后按"这一档上次选的"恢复
    book = web_runtime.provider_book()
    assert book.last_efforts.get(active_id) == "high", "思考档必须落盘，否则重启就丢"
    assert book.last_models.get(active_id) == "deepseek-v4.1-flash-1017"

    # ⑥ 删掉这一档 → 回落到还能用的那一档（内置千问）
    rev = (await srv.get_providers())["revision"]
    await srv.delete_provider(active_id, revision=rev)
    after = await srv.get_providers()
    assert active_id not in [p["id"] for p in after["providers"]]
    assert after["active"] == "qwen"
