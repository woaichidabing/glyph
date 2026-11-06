"""自定义模型端点 / 配置来源测试（从 test_server.py 拆出）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.server as srv
from web import runtime as web_runtime
from web.deps import state as web_state


def _stub_rebuild(monkeypatch):
    """把"整体重建 runtime / loop"换成桩并捕获传入的 settings。"""
    captured: dict = {}

    def _fake_build(ns):
        captured["settings"] = ns
        return (
            object(),
            web_state.registry,
            "sys",
            object(),
            None,
            None,
            object(),
            object(),
            object(),
        )

    def _fake_loop(ns, llm, reg, hist, compactor=None, compaction_observer=None):
        return SimpleNamespace(busy=False)

    monkeypatch.setattr(web_runtime, "_build_runtime", _fake_build)
    monkeypatch.setattr(web_runtime, "_make_loop", _fake_loop)
    return captured


# ---------- 模型来源两档（默认千问 / 自定义 OpenAI 兼容端点）：保存落盘 + 叠加 + 回显 ----------




# ---------- 模型来源两档（默认千问 / 自定义 OpenAI 兼容端点）：保存落盘 + 叠加 + 回显 ----------


def _isolate_provider(monkeypatch, tmp_path) -> Path:
    """供应商配置落进临时文件并重置模块级来源状态，绝不写真实 ~/.markr。"""
    cfg_path = tmp_path / "provider.json"
    monkeypatch.setenv("GLYPH_PROVIDER_CONFIG", str(cfg_path))
    monkeypatch.setattr(web_state, "provider", srv.ProviderConfig())
    return cfg_path


async def test_get_config_reports_default_source_section(web_env, monkeypatch, tmp_path):
    _isolate_provider(monkeypatch, tmp_path)
    resp = await srv.get_config()
    assert resp["source"] == "default"
    assert resp["provider"] == "dashscope"  # 旧字段在默认档保持不变
    assert resp["custom"] == {
        "base_url": "",
        "has_key": False,
        "models": [],
        "selected_model": "",
        "details": [],
    }


async def test_supports_vision_true_by_default(web_env, monkeypatch, tmp_path):
    """默认千问档带视觉子模型：图片能力开（前端图片入口可用）。"""
    _isolate_provider(monkeypatch, tmp_path)
    resp = await srv.get_config()
    assert resp["supports_vision"] is True


async def test_custom_text_only_provider_reports_no_vision(web_env, monkeypatch, tmp_path):
    """自定义清单全是纯文本模型：supports_vision=False，前端据此禁用图片上传入口。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["gpt-3.5-turbo"],
                    selected_model="gpt-3.5-turbo",
                ),
            )
        )
    )
    resp = await srv.get_config()
    assert resp["supports_vision"] is False


async def test_custom_vision_provider_reports_vision(web_env, monkeypatch, tmp_path):
    """自定义清单里有视觉模型（即便主脑选了纯文本）：图片能力仍开。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["gpt-3.5-turbo", "gpt-4o"],
                    selected_model="gpt-3.5-turbo",
                ),
            )
        )
    )
    resp = await srv.get_config()
    assert resp["supports_vision"] is True


async def test_save_custom_provider_persists_and_applies(
    web_env, monkeypatch, tmp_path
):
    cfg_path = _isolate_provider(monkeypatch, tmp_path)
    captured = _stub_rebuild(monkeypatch)

    resp = await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["gpt-4o", "claude-3-5-sonnet"],
                    selected_model="gpt-4o",
                ),
            )
        )
    )

    # 运行时 settings 已整体指向自定义端点与选中模型，重建用的就是它
    assert web_state.settings.base_url == "https://api.example.com/v1"
    assert web_state.settings.api_key == "sk-custom"
    assert web_state.settings.model == "gpt-4o"
    assert captured["settings"].base_url == "https://api.example.com/v1"
    # 回显：来源 / 清单 / 选中回来，只回 has_key，绝不回明文 key
    assert resp["source"] == "custom"
    assert resp["custom"]["models"] == ["gpt-4o", "claude-3-5-sonnet"]
    assert resp["custom"]["selected_model"] == "gpt-4o"
    assert resp["custom"]["has_key"] is True
    assert "api_key" not in resp["custom"]
    assert "sk-custom" not in str(resp)
    # custom 段顺带每个已存模型的能力（与 models 同序），设置页回显分组 / 选择器显隐用
    details = resp["custom"]["details"]
    assert [d["id"] for d in details] == ["gpt-4o", "claude-3-5-sonnet"]
    assert all(d["supports_vision"] is True for d in details)
    # 落盘可重新读回（重启后能恢复同样的来源）
    persisted = srv.load_provider_config(cfg_path)
    assert persisted.source == "custom"
    assert persisted.custom.base_url == "https://api.example.com/v1"
    assert persisted.custom.models == ("gpt-4o", "claude-3-5-sonnet")


async def test_save_custom_provider_missing_url_is_400(
    web_env, monkeypatch, tmp_path
):
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.set_config(
            srv.ConfigRequest(
                provider=srv.ProviderIn(
                    source="custom",
                    custom=srv.CustomProviderIn(api_key="k", models=["gpt-4o"]),
                )
            )
        )
    assert ei.value.status_code == 400


async def test_switch_back_to_default_restores_official_url_but_keeps_custom(
    web_env, monkeypatch, tmp_path
):
    from glyph.config import DEFAULT_BASE_URL

    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://x/v1",
                    api_key="k",
                    models=["gpt-4o"],
                    selected_model="gpt-4o",
                ),
            )
        )
    )
    resp = await srv.set_config(
        srv.ConfigRequest(provider=srv.ProviderIn(source="default", default_api_key="sk-qwen"))
    )
    assert resp["source"] == "default"
    assert web_state.settings.base_url == DEFAULT_BASE_URL  # 地址拉回官方千问
    assert web_state.settings.api_key == "sk-qwen"
    # 自定义信息不抹掉，用户再切回去还在
    assert resp["custom"]["base_url"] == "https://x/v1"
    assert resp["custom"]["models"] == ["gpt-4o"]


async def test_switch_back_to_default_restores_builtin_submodels(
    web_env, monkeypatch, tmp_path
):
    """从自定义档切回默认：看图 / 批改 / 润色 / 摘要 / 兜底链必须整体恢复内置，
    不能把自定义档的污染（看图置空、子模型指向第三方名、兜底链清空）带回来。"""
    from glyph.config import load_settings

    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    # 全是纯文本：自定义档会把看图置空、子模型全压到主模型、兜底链清空
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://x/v1",
                    api_key="k",
                    models=["gpt-3.5-turbo", "deepseek-chat"],
                    selected_model="deepseek-chat",
                ),
            )
        )
    )
    assert web_state.settings.vision_model == ""  # 先确认自定义档确实污染了

    resp = await srv.set_config(
        srv.ConfigRequest(provider=srv.ProviderIn(source="default"))
    )

    # 切回默认 = 回到干净 .env 打底的整套内置配置（.env 可覆盖兜底常量，故对照 load_settings）
    fresh = load_settings()
    s = web_state.settings
    assert s.model == fresh.model
    assert s.grade_model == fresh.grade_model
    assert s.polish_model == fresh.polish_model
    assert s.compact_model == fresh.compact_model
    assert s.vision_model == fresh.vision_model
    assert s.grade_fallback_models == fresh.grade_fallback_models
    assert s.main_fallback_models == fresh.main_fallback_models
    # 第三方模型名 / 空看图 / 空兜底链一个都不许残留
    assert s.model != "deepseek-chat"
    assert s.vision_model.strip() != ""
    assert s.main_fallback_models
    # 顶层看图能力随之恢复，前端图片入口重新可用
    assert resp["supports_vision"] is True


async def test_custom_model_in_catalog_accepted_unknown_rejected(
    web_env, monkeypatch, tmp_path
):
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://x/v1",
                    api_key="k",
                    models=["gpt-4o", "gpt-4o-mini"],
                    selected_model="gpt-4o",
                ),
            )
        )
    )
    # 清单内模型可切换，且选中被持久化（重启后仍是它）
    await srv.set_config(srv.ConfigRequest(model="gpt-4o-mini"))
    assert web_state.settings.model == "gpt-4o-mini"
    assert web_state.provider.custom.selected_model == "gpt-4o-mini"
    # 既不在内置表、也不在自定义清单 → 400
    with pytest.raises(HTTPException) as ei:
        await srv.set_config(srv.ConfigRequest(model="totally-made-up"))
    assert ei.value.status_code == 400


async def test_legacy_plain_api_key_saved_as_default_provider(
    web_env, monkeypatch, tmp_path
):
    cfg_path = _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(srv.ConfigRequest(api_key="sk-legacy"))
    assert web_state.provider.source == "default"
    assert web_state.provider.default_api_key == "sk-legacy"
    # 2026-10-28 起：明文钥匙只进凭据库；配置文件里的兼容键留空
    assert srv.load_provider_config(cfg_path).default_api_key == ""
    from glyph.credentials import CredentialStore

    assert CredentialStore().resolve("QWEN_API_KEY") == "sk-legacy"


# ---------- 一键拉取自定义端点上游模型（只读探测，不保存） ----------


async def test_fetch_models_success_strips_inputs(web_env, monkeypatch) -> None:
    captured: dict = {}

    async def fake_fetch(base_url, api_key, **kwargs):
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return ("gpt-4o", "gpt-4o-mini")

    monkeypatch.setattr(web_runtime, "fetch_upstream_models", fake_fetch)
    resp = await srv.fetch_models(
        srv.FetchModelsIn(base_url="  https://x/v1/  ", api_key="  sk  ")
    )
    assert resp["models"] == ["gpt-4o", "gpt-4o-mini"]
    assert resp["count"] == 2
    # 同时回传每个模型的能力（与 models 同序），前端据此分组 / 隐藏非对话 / 选择器显隐
    details = resp["details"]
    assert [d["id"] for d in details] == ["gpt-4o", "gpt-4o-mini"]
    assert all({"id", "brand", "context_window", "reasoning_levels",
                "supports_vision", "can_be_main"} <= set(d) for d in details)
    # 传给探测层的地址 / 密钥已去空白
    assert captured == {"base_url": "https://x/v1/", "api_key": "sk"}


async def test_fetch_models_details_mark_vision_and_non_chat(web_env, monkeypatch) -> None:
    async def fake_fetch(base_url, api_key, **kwargs):
        # gpt-4o 能看图且能当大脑；gpt-3.5-turbo 纯文本对话；tts-1 是非对话模型
        return ("gpt-4o", "gpt-3.5-turbo", "tts-1")

    monkeypatch.setattr(web_runtime, "fetch_upstream_models", fake_fetch)
    resp = await srv.fetch_models(
        srv.FetchModelsIn(base_url="https://x/v1", api_key="sk")
    )
    by_id = {d["id"]: d for d in resp["details"]}
    assert by_id["gpt-4o"]["supports_vision"] is True
    assert by_id["gpt-4o"]["can_be_main"] is True
    assert by_id["gpt-3.5-turbo"]["supports_vision"] is False
    assert by_id["gpt-3.5-turbo"]["can_be_main"] is True
    assert by_id["tts-1"]["can_be_main"] is False
    assert by_id["tts-1"]["supports_vision"] is False


async def test_fetch_models_discovery_error_is_400(web_env, monkeypatch) -> None:
    async def boom(base_url, api_key, **kwargs):
        raise srv.ProviderDiscoverError("API Key 不对或没有权限", kind="auth")

    monkeypatch.setattr(web_runtime, "fetch_upstream_models", boom)
    with pytest.raises(HTTPException) as ei:
        await srv.fetch_models(srv.FetchModelsIn(base_url="https://x/v1", api_key="k"))
    assert ei.value.status_code == 400
    assert "API Key" in ei.value.detail


async def test_fetch_models_missing_field_is_400(web_env) -> None:
    with pytest.raises(HTTPException) as ei:
        await srv.fetch_models(srv.FetchModelsIn(base_url="   ", api_key="k"))
    assert ei.value.status_code == 400


async def test_fetch_models_empty_list_is_400(web_env, monkeypatch) -> None:
    async def empty(base_url, api_key, **kwargs):
        return ()

    monkeypatch.setattr(web_runtime, "fetch_upstream_models", empty)
    with pytest.raises(HTTPException) as ei:
        await srv.fetch_models(srv.FetchModelsIn(base_url="https://x/v1", api_key="k"))
    assert ei.value.status_code == 400
