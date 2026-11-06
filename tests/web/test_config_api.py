"""模型 / 配置接口测试（从 test_server.py 拆出）。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.server as srv
from web import runtime as web_runtime
from web.deps import state as web_state

# ---------- 装配：真工具注册（MSG-010 ①） ----------


def test_build_runtime_registers_real_tools():
    settings = srv.load_settings()
    (
        llm,
        registry,
        system_prompt,
        injector,
        compactor,
        observer,
        _scheduler,
        _extractor,
        _consolidator,
    ) = web_runtime._build_runtime(settings)
    names = {s["function"]["name"] for s in registry.schemas()}
    assert {"grade_text", "ocr_image", "recheck_detection"} <= names
    # 记忆三件套同样注册进表
    assert {"remember", "forget", "read_memory"} <= names
    # T07 练习尝试记录三件套同样必须在 web 运行时注册（server 装配漏提交会直接红）
    assert {"record_attempt", "read_attempts", "delete_attempt"} <= names
    assert injector is not None
    # #66 上下文压缩：auto_compact 开则装配 L3 写手；D2 遥测观察者始终可调用
    assert (compactor is not None) is settings.auto_compact
    assert callable(observer)
    # 重建的系统提示词必须包含所有已注册工具，否则多余工具对模型是"死的"
    for n in names:
        assert n in system_prompt


def test_build_runtime_intersects_web_switch_with_brain_capability():
    # 选了「不挂平台联网」的 flash：即便全局开关开着，构造出的 LLM 也不下发 web_search
    s = replace(srv.load_settings(), web_search=True, model="qwen3.8-flash")
    llm, *_ = web_runtime._build_runtime(s)
    assert llm.web_search is False
    # 在表且自身支持联网的型号 + 开关开：保持联网
    s = replace(srv.load_settings(), web_search=True, model="qwen3.8-max")
    llm, *_ = web_runtime._build_runtime(s)
    assert llm.web_search is True
    # 开关关：哪怕型号能联网也不联网
    s = replace(srv.load_settings(), web_search=False, model="qwen3.8-max")
    llm, *_ = web_runtime._build_runtime(s)
    assert llm.web_search is False


# ---------- /api/tools：下发真实模型名（web-ui-spec G5） ----------


async def test_tools_endpoint_reports_models(web_env):
    """G5：前端 model-tag / OCR 台 meta 不再写死，模型名由 /api/tools 下发。"""
    s = srv.load_settings()
    resp = await srv.list_tools()
    assert resp["models"] == {"chat": s.model, "grade": s.grade_model, "vision": s.vision_model}
    assert isinstance(resp["tools"], list)


# ---------- 模型选择器：能力清单 + /api/config 换主脑 / 思考档（只换大脑，不碰子模型） ----------


def _isolate_provider(monkeypatch, tmp_path) -> Path:
    """供应商配置落进临时文件并重置模块级来源状态，绝不写真实 ~/.markr。"""
    cfg_path = tmp_path / "provider.json"
    monkeypatch.setenv("GLYPH_PROVIDER_CONFIG", str(cfg_path))
    monkeypatch.setattr(web_state, "provider", srv.ProviderConfig())
    return cfg_path


def _stub_rebuild(monkeypatch):
    """把"整体重建 runtime / loop"换成桩并捕获传入的 settings（web 层不重复测真装配）。"""
    captured: dict = {}

    def _fake_build(ns):
        captured["settings"] = ns
        # 返回值要与 web/server.py 的 _build_runtime 一一对应：
        # llm / registry / system_prompt / injector / compactor / observer
        # + 记忆 v2 三件套（调度器 / 提炼器 / 整理器）
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


async def test_get_models_lists_all_brains_with_levels(web_env):
    resp = await srv.list_models()

    ids = {m["id"] for m in resp["models"]}
    assert ids == {
        "qwen3.8-max",
        "deepseek-v4-pro-0813",
        "glm-5.2",
        "glm-5.3",
        "deepseek-v4.1-flash",
        "qwen3.8-flash",
        "qwen3.7-flash-2026-07-15",
        "kimi-k3",
        "qwen3.8-27b",
    }
    # 默认大脑必须真实存在于菜单，且指向当前实测可用型号（不是额度耗尽的旗舰）
    assert resp["default"] == "deepseek-v4.1-flash"
    assert resp["default"] in ids
    for m in resp["models"]:
        # 菜单纯模型名；思考档至少含 low/medium、取值只来自三档；有正的上下文窗口（环的分母）
        assert m["label"] == m["id"]
        assert "low" in m["reasoning_levels"] and "medium" in m["reasoning_levels"]
        assert set(m["reasoning_levels"]) <= {"low", "medium", "high"}
        assert m["context_window"] > 0
        assert m["desc"].strip()


async def test_list_models_custom_returns_custom_catalog(web_env, monkeypatch, tmp_path):
    """自定义档：菜单回该档**全部**模型（同一张皮），带能力字段，内置模型不混排。

    2026-10-27 口径变更：**不再按"能不能当大脑"过滤**。用户在清单里填了什么就列什么，
    只填一个看图模型也照样能当主模型——选错了是他自己的判断，产品不替他挡。
    所以 tts-1 这类明显不是对话模型的条目同样在列。
    """
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["gpt-4o", "deepseek-chat", "tts-1"],
                    selected_model="gpt-4o",
                ),
            )
        )
    )

    resp = await srv.list_models()
    ids = [m["id"] for m in resp["models"]]
    # 顺序沿用用户保存顺序；不再剔除 can_be_main=False 的型号（tts-1 也在列）
    assert ids == ["gpt-4o", "deepseek-chat", "tts-1"]
    assert resp["default"] == "gpt-4o"  # 默认指向用户选中的首选
    by = {m["id"]: m for m in resp["models"]}
    gpt, deep = by["gpt-4o"], by["deepseek-chat"]
    assert gpt["label"] == "gpt-4o"
    assert gpt["supports_vision"] is True and gpt["brand"] == "openai"
    assert deep["supports_vision"] is False and deep["brand"] == "deepseek"
    # 自定义端点不臆造联网能力；上下文窗口为正（用量环分母）；思考档是列表
    assert gpt["web_search"] is False
    assert gpt["context_window"] > 0 and deep["context_window"] > 0
    assert isinstance(gpt["reasoning_levels"], list)
    assert "看图" in gpt["desc"]  # 能看图的模型，描述里用一句人话点明
    # 内置模型不得与自定义模型混排在同一张菜单里
    assert "deepseek-v4.1-flash" not in ids


async def test_get_models_builtin_items_carry_brand_and_vision(web_env):
    """内置档每个菜单项都要下发 brand / supports_vision，不能让前端靠"缺字段"兜底。

    背景：内置档只发了 id/label/desc/reasoning_levels/web_search/context_window 六项，
    前端 normalizeModel 只能把 brand 兜成空串，品牌被迫在 brainLogo.ts 里按 id 前缀
    再猜一遍（同一件事两处判定，必然漂移）。看图能力由独立的 vision 子模型负责，
    内置主脑如实标 False —— 依据 provider/capabilities.py::capabilities_for 的刻意设计。
    """
    resp = await srv.list_models()

    for m in resp["models"]:
        assert m["brand"], f"内置档 {m['id']} 未下发 brand（前端只能空串兜底）"
        assert m["supports_vision"] is False, f"内置主脑 {m['id']} 不应标视觉"


async def test_builtin_and_custom_brain_items_share_same_shape(web_env, monkeypatch, tmp_path):
    """两档菜单必须是同一张皮：字段集合完全一致，加字段就得两边一起加。"""
    builtin = await srv.list_models()

    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["gpt-4o"],
                    selected_model="gpt-4o",
                ),
            )
        )
    )
    custom = await srv.list_models()

    assert set(builtin["models"][0]) == set(custom["models"][0])


async def test_get_config_reports_reasoning_effort(web_env):
    resp = await srv.get_config()
    assert resp["reasoning_effort"] == web_state.settings.reasoning_effort


async def test_set_config_switches_brain_without_touching_key_or_submodels(web_env, monkeypatch):
    captured = _stub_rebuild(monkeypatch)
    before = web_state.settings

    resp = await srv.set_config(srv.ConfigRequest(model="qwen3.8-max", reasoning_effort="high"))

    # 大脑与思考档换了；这次没带 key，钥匙必须原样保留
    assert web_state.settings.model == "qwen3.8-max"
    assert web_state.settings.reasoning_effort == "high"
    assert web_state.settings.api_key == before.api_key
    # 重建 runtime 用的就是换脑之后的 settings（否则换脑不生效）
    assert captured["settings"].model == "qwen3.8-max"
    assert captured["settings"].reasoning_effort == "high"
    # 回给前端的状态同步
    assert resp["model"] == "qwen3.8-max"
    assert resp["reasoning_effort"] == "high"
    # /api/tools 的 chat 跟随大脑；批改 / 看图子模型纹丝不动（用户不可见、不可选）
    models = (await srv.list_tools())["models"]
    assert models["chat"] == "qwen3.8-max"
    assert models["grade"] == before.grade_model
    assert models["vision"] == before.vision_model


async def test_set_config_changes_effort_alone_keeps_model(web_env, monkeypatch):
    # 同一型号只换档：当前大脑先摆成菜单内型号，再只发 effort
    monkeypatch.setattr(web_state, "settings", replace(web_state.settings, model="glm-5.2"))
    _stub_rebuild(monkeypatch)

    await srv.set_config(srv.ConfigRequest(reasoning_effort="low"))

    assert web_state.settings.model == "glm-5.2"
    assert web_state.settings.reasoning_effort == "low"


async def test_set_config_rejects_unknown_model(web_env):
    before = web_state.settings.model
    with pytest.raises(HTTPException) as ei:
        await srv.set_config(srv.ConfigRequest(api_key="k", model="gpt-4-turbo"))
    assert ei.value.status_code == 400
    assert web_state.settings.model == before  # 被拒后配置不变


async def test_set_config_rejects_effort_unsupported_by_model(web_env):
    # 非法档位（也覆盖"型号不支持该档"的校验路径：当前菜单型号都支持三档，
    # 故用一个不存在的档位验证拒绝逻辑，绝不把不认的档位发出去）
    with pytest.raises(HTTPException) as ei:
        await srv.set_config(
            srv.ConfigRequest(api_key="k", model="qwen3.8-max", reasoning_effort="ultra")
        )
    assert ei.value.status_code == 400
    assert web_state.settings.reasoning_effort != "ultra"


async def test_set_config_explicit_empty_key_is_400(web_env):
    # 兼容旧前端：显式传空钥匙仍 400；但不传钥匙（只切模型）是允许的（见换脑用例）
    with pytest.raises(HTTPException) as ei:
        await srv.set_config(srv.ConfigRequest(api_key="   "))
    assert ei.value.status_code == 400


async def test_set_config_while_busy_is_409(web_env, monkeypatch):
    monkeypatch.setattr(web_state, "loop", SimpleNamespace(busy=True))
    with pytest.raises(HTTPException) as ei:
        await srv.set_config(srv.ConfigRequest(model="qwen3.8-max"))
    assert ei.value.status_code == 409


async def test_get_config_reports_context_mode(web_env):
    resp = await srv.get_config()
    assert resp["context_mode"] == web_state.settings.context_mode


async def test_set_config_switches_context_mode_to_max(web_env, monkeypatch):
    # 只切档位：进 settings、用于重建 runtime / loop（环分母与压缩阈值都按它），回体同步。
    captured = _stub_rebuild(monkeypatch)
    before = web_state.settings

    resp = await srv.set_config(srv.ConfigRequest(context_mode="max"))

    assert web_state.settings.context_mode == "max"
    assert captured["settings"].context_mode == "max"
    assert resp["context_mode"] == "max"
    # 只切档位不动大脑 / 钥匙 / 思考档
    assert web_state.settings.model == before.model
    assert web_state.settings.api_key == before.api_key


async def test_set_config_context_mode_dirty_value_falls_back_to_standard(web_env, monkeypatch):
    # 档位只有 standard/max，脏值 / 大小写由 normalize 静默回退标准档（与环境变量同一套容错）
    _stub_rebuild(monkeypatch)
    resp = await srv.set_config(srv.ConfigRequest(context_mode=" ultra "))
    assert web_state.settings.context_mode == "standard"
    assert resp["context_mode"] == "standard"


async def test_get_config_reports_speaking_style(web_env):
    resp = await srv.get_config()
    assert resp["speaking_style"] == web_state.settings.speaking_style


async def test_set_config_switches_speaking_style(web_env, monkeypatch):
    # 只切说话风格：进 settings、随 runtime 重建换系统提示词口吻段，回体同步；不动大脑 / 钥匙。
    captured = _stub_rebuild(monkeypatch)
    before = web_state.settings

    resp = await srv.set_config(srv.ConfigRequest(speaking_style="witty"))

    assert web_state.settings.speaking_style == "witty"
    assert captured["settings"].speaking_style == "witty"
    assert resp["speaking_style"] == "witty"
    assert web_state.settings.model == before.model
    assert web_state.settings.api_key == before.api_key


async def test_set_config_speaking_style_dirty_value_falls_back_to_natural(web_env, monkeypatch):
    # 风格只有 natural/witty/calm，脏值 / 大小写由 normalize 静默回退自然档
    _stub_rebuild(monkeypatch)
    resp = await srv.set_config(srv.ConfigRequest(speaking_style=" cheeky "))
    assert web_state.settings.speaking_style == "natural"
    assert resp["speaking_style"] == "natural"


# ---------- 三能力槽（role_models）：绑定 / 回显 / 引用修复（2026-10-13） ----------


async def test_get_config_reports_roles_default_tier(web_env):
    """默认档：chat 绑定=主脑；vision/grade 未指定时 effective 落在内置推荐（env/默认值）。"""
    resp = await srv.get_config()
    roles = resp["roles"]
    assert roles["chat"]["effective"] == web_state.settings.model
    assert roles["vision"]["bound"] is None
    assert roles["vision"]["effective"] == web_state.settings.vision_model
    assert roles["vision"]["available"] is True
    assert roles["grade"]["bound"] is None
    assert roles["grade"]["effective"] == web_state.settings.grade_model
    assert resp["role_fallbacks"] == []


async def test_set_config_default_tier_role_models_override_submodels(  # noqa: E501
    web_env, monkeypatch, tmp_path
):
    """默认档：显式指定的批改/看图覆盖内置推荐，且落盘（重启恢复）；没指定的槽不动。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    resp = await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="default",
                role_models=srv.RoleModelsIn(vision="qwen3-vl-plus", grade="glm-5.2"),
            )
        )
    )
    assert web_state.settings.vision_model == "qwen3-vl-plus"
    assert web_state.settings.grade_model == "glm-5.2"
    assert resp["roles"]["vision"]["bound"] == "qwen3-vl-plus"
    assert resp["roles"]["vision"]["effective"] == "qwen3-vl-plus"
    assert resp["roles"]["grade"]["effective"] == "glm-5.2"
    # 落盘：重读 provider.json 仍在
    saved = srv.load_provider_config()
    assert saved.role_models.vision == "qwen3-vl-plus"
    assert saved.role_models.grade == "glm-5.2"


async def test_set_config_role_models_clear_binding_with_null(web_env, monkeypatch, tmp_path):
    """显式 null = 清除绑定回自动；只带的字段才改（exclude_unset 语义）。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="default",
                role_models=srv.RoleModelsIn(vision="qwen3-vl-plus", grade="glm-5.2"),
            )
        )
    )
    resp = await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(source="default", role_models=srv.RoleModelsIn(grade=None))
        )
    )
    assert resp["roles"]["grade"]["bound"] is None
    assert resp["roles"]["grade"]["effective"] == web_state.settings.grade_model
    # vision 的绑定没被顺手清掉
    assert resp["roles"]["vision"]["bound"] == "qwen3-vl-plus"


async def test_custom_role_models_bind_grade_and_auto_vision(web_env, monkeypatch, tmp_path):
    """自定义档：批改显式绑定生效；看图未指定 = 清单里第一个能看图的（自动适配）。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    resp = await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["deepseek-chat", "gpt-4o"],
                    selected_model="deepseek-chat",
                ),
                role_models=srv.RoleModelsIn(grade="gpt-4o"),
            )
        )
    )
    assert web_state.settings.model == "deepseek-chat"
    assert web_state.settings.grade_model == "gpt-4o"
    assert web_state.settings.vision_model == "gpt-4o"  # 自动适配：清单里第一个能看图的
    roles = resp["roles"]
    assert roles["chat"]["bound"] == "deepseek-chat"
    assert roles["grade"]["bound"] == "gpt-4o"
    assert roles["vision"]["bound"] is None
    assert roles["vision"]["effective"] == "gpt-4o"
    assert roles["vision"]["available"] is True


async def test_custom_pure_text_endpoint_reports_vision_unavailable(web_env, monkeypatch, tmp_path):
    """纯文本端点：vision.available=False（前端据全局 supports_vision 隐藏图片入口，同一口径）。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    resp = await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["deepseek-chat"],
                    selected_model="deepseek-chat",
                ),
            )
        )
    )
    roles = resp["roles"]
    assert roles["vision"]["available"] is False
    assert roles["vision"]["effective"] is None
    assert resp["supports_vision"] is False


async def test_custom_refetch_dangling_binding_repairs_with_fallbacks(
    web_env, monkeypatch, tmp_path
):
    """引用修复：绑定指向的模型从清单消失 → 置回自动 + role_fallbacks 带回响应（只报一次）。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["deepseek-chat", "qv-72b", "gpt-4o"],
                    selected_model="deepseek-chat",
                ),
                role_models=srv.RoleModelsIn(vision="qv-72b", grade="gpt-4o"),
            )
        )
    )
    assert web_state.settings.vision_model == "qv-72b"

    # 重新获取：新清单不再包含 qv-72b → vision 绑定失效
    resp = await srv.set_config(
        srv.ConfigRequest(
            provider=srv.ProviderIn(
                source="custom",
                custom=srv.CustomProviderIn(
                    base_url="https://api.example.com/v1",
                    api_key="sk-custom",
                    models=["deepseek-chat", "gpt-4o"],
                    selected_model="deepseek-chat",
                ),
            )
        )
    )
    assert resp["role_fallbacks"] == ["vision"]
    assert resp["roles"]["vision"]["bound"] is None
    assert resp["roles"]["vision"]["effective"] == "gpt-4o"
    assert web_state.settings.vision_model == "gpt-4o"
    # grade 绑定还在清单内，不受牵连
    assert resp["roles"]["grade"]["bound"] == "gpt-4o"
    # GET 不再带黄条信号（role_fallbacks 只在保存响应里出现一次）
    followup = await srv.get_config()
    assert followup["role_fallbacks"] == []


async def test_role_models_change_rejects_while_busy(web_env, monkeypatch, tmp_path):
    """改绑定也走整体重建：agent 忙碌时同样 409，和换钥匙 / 换主脑同一道闸。"""
    _isolate_provider(monkeypatch, tmp_path)
    _stub_rebuild(monkeypatch)
    monkeypatch.setattr(web_state.loop, "busy", True)
    try:
        with pytest.raises(HTTPException) as ei:
            await srv.set_config(
                srv.ConfigRequest(
                    provider=srv.ProviderIn(
                        source="default", role_models=srv.RoleModelsIn(grade="glm-5.2")
                    )
                )
            )
        assert ei.value.status_code == 409
    finally:
        monkeypatch.setattr(web_state.loop, "busy", False)


async def test_supports_vision_true_when_main_model_sees_images(web_env, monkeypatch):
    """主模型自己能看图时，也不该被判"看不了"（2026-10-31 合并口径）。"""
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(
        web_state,
        "settings",
        dc_replace(web_state.settings, vision_model="", main_model_can_see_image=True),
    )
    resp = await srv.get_config()
    assert resp["supports_vision"] is True


async def test_supports_vision_false_when_neither(web_env, monkeypatch):
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(
        web_state,
        "settings",
        dc_replace(web_state.settings, vision_model="", main_model_can_see_image=False),
    )
    resp = await srv.get_config()
    assert resp["supports_vision"] is False