"""供应商多档接口测试（v2：多档并存 + 只能启用一档 + 模型入口只列当前档）。

产品口径（2026-10-27 定稿）：
1. 几档供应商并存，同一时刻只能启用一档；
2. 主界面的模型入口只列「当前启用那一档」的模型、不显示供应商名、不按 can_be_main 过滤；
3. 没配好的档（缺地址 / 缺密钥 / 空模型）既不显示也不能启用。

写法与 test_config_api.py 一致：直接调用路由函数（不起 HTTP 服务），
整体重建换成桩并捕获——web 层不重复测真装配。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.server as srv
from web import runtime as web_runtime
from web.deps import state as web_state

# --------------------------------------------------------------- 测试脚手架


def _stub_rebuild(monkeypatch) -> dict:
    """把"整体重建运行时 / 重挂循环"换成桩，并捕获每次重建用的 settings。"""
    captured: dict = {"builds": []}

    def _fake_build(ns):
        captured["builds"].append(ns)
        # 返回值必须与 runtime._build_runtime 一一对应：llm / registry / system_prompt /
        # injector / compactor / observer + 记忆 v2 三件套
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


def _provider_file() -> Path:
    """当前用例的供应商配置文件（web_env + conftest 已隔离到 tmp_path）。"""
    return Path(os.environ["GLYPH_PROVIDER_CONFIG"])


def _write_raw_book(providers: list[dict], active: str = "qwen") -> None:
    """直接往文件里写一份 v2 配置（模拟"用户手改过 / 上一版留下的"脏目录）。"""
    path = _provider_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": 2, "active": active, "providers": providers, "last_models": {}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _custom_entry(**over: object) -> srv.ProviderEntryIn:
    """一份合法的自定义档请求（前置数据：地址 + 密钥 + 两个模型）。"""
    data: dict = {
        "name": "DeepSeek Cloud",
        "kind": "custom",
        "api": "openai-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-deepseek",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "roles": {},
    }
    data.update(over)
    return srv.ProviderEntryIn(**data)


def _by_id(payload: dict) -> dict:
    return {p["id"]: p for p in payload["providers"]}


# --------------------------------------------------------------- GET /api/providers


async def test_get_providers_starts_with_only_builtin_tier(web_env):
    """全新安装：只剩内置千问那一档，且它就是当前启用档。"""
    resp = await srv.get_providers()

    assert resp["active"] == "qwen"
    assert [p["id"] for p in resp["providers"]] == ["qwen"]
    qwen = resp["providers"][0]
    assert qwen["kind"] == "builtin"
    assert qwen["is_active"] is True
    # 内置档的模型由实测能力表现填（不落库），地址是固定端点
    assert len(qwen["models"]) == len(web_runtime.MAIN_MODELS)
    assert qwen["base_url"] == web_runtime.DEFAULT_BASE_URL
    # 模型行的字段与自定义档同一套形状
    assert set(qwen["models"][0]) == {
        "id",
        "label",
        "context_window",
        "max_output",
        "inputs",
        # 2026-10-27：档位可扩到极高/最高，两档都必须带上这一格（内置只读、自定义可勾）
        "reasoning_levels",
    }


async def test_get_providers_never_leaks_api_key(web_env, monkeypatch):
    """对外只说"配没配"（has_key），绝不回明文密钥。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())

    resp = await srv.get_providers()
    deep = _by_id(resp)["deepseek-cloud"]
    assert deep["has_key"] is True
    assert "api_key" not in deep
    # 整包序列化里也不能出现那把钥匙
    assert "sk-deepseek" not in json.dumps(resp, ensure_ascii=False)


async def test_get_providers_hides_tier_that_is_not_configured(web_env):
    """没配好的档（缺地址 / 缺密钥 / 空模型）不显示——判定只有 validate_entry 一处。"""
    _write_raw_book(
        providers=[
            {"id": "qwen", "name": "千问（DashScope）", "kind": "builtin"},
            {
                "id": "no-url",
                "name": "缺地址",
                "kind": "custom",
                "api_key": "sk-x",
                "models": ["m1"],
            },
            {
                "id": "no-key",
                "name": "缺密钥",
                "kind": "custom",
                "base_url": "https://api.example.com/v1",
                "models": ["m1"],
            },
            {
                "id": "no-model",
                "name": "空模型",
                "kind": "custom",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-x",
                "models": [],
            },
        ]
    )

    resp = await srv.get_providers()
    assert [p["id"] for p in resp["providers"]] == ["qwen"]
    assert resp["active"] == "qwen"


# --------------------------------------------------------------- POST /api/providers


async def test_post_provider_creates_tier_and_first_tier_becomes_active(web_env, monkeypatch):
    """第一档自定义供应商配好后自动成为当前启用，并立刻重建运行时（不重启就生效）。"""
    captured = _stub_rebuild(monkeypatch)

    resp = await srv.set_provider(_custom_entry())

    assert resp["ok"] is True
    assert resp["provider_id"] == "deepseek-cloud"  # id 由名字派生
    assert resp["active"] == "deepseek-cloud"
    deep = _by_id(resp)["deepseek-cloud"]
    assert deep["is_active"] is True
    assert [m["id"] for m in deep["models"]] == ["deepseek-chat", "deepseek-reasoner"]
    # 落盘了（重新从磁盘读得到），且写成功后走了一次整体重建
    book = web_runtime.provider_book()
    assert book.active == "deepseek-cloud"
    assert book.find("deepseek-cloud").api_key == "sk-deepseek"
    assert len(captured["builds"]) == 1
    # 运行时配置真的换成这一档了（模型取清单第一个），不是只改了个界面数字
    assert web_state.settings.model == "deepseek-chat"


async def test_post_provider_rejects_unconfigured_tier_without_saving(web_env, monkeypatch):
    """缺密钥 → 400 + 中文原因；**不落库、不重建**（没配好的档连"存在"都不算）。"""
    captured = _stub_rebuild(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        await srv.set_provider(_custom_entry(api_key=""))
    assert ei.value.status_code == 400
    assert "密钥" in ei.value.detail
    assert captured["builds"] == []
    assert [p["id"] for p in (await srv.get_providers())["providers"]] == ["qwen"]


async def test_post_provider_rejects_empty_model_list(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.set_provider(_custom_entry(models=[]))
    assert ei.value.status_code == 400
    assert "模型" in ei.value.detail


async def test_post_provider_keeps_saved_key_and_models_when_omitted(web_env, monkeypatch):
    """改个名字不该把密钥 / 模型清单清空（缺省 = 沿用已存的值）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())

    resp = await srv.set_provider(srv.ProviderEntryIn(id="deepseek-cloud", name="深度求索"))

    deep = _by_id(resp)["deepseek-cloud"]
    assert deep["name"] == "深度求索"
    assert deep["has_key"] is True
    assert [m["id"] for m in deep["models"]] == ["deepseek-chat", "deepseek-reasoner"]
    # 更新而不是新增（还是两档：内置 + 这一档）
    assert len(resp["providers"]) == 2


async def test_post_provider_same_name_updates_in_place(web_env, monkeypatch):
    """没带 id 时按名字派生：同名再次保存 = 更新同一档，不会越保存越多。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())
    resp = await srv.set_provider(_custom_entry(base_url="https://api.deepseek.com/v2"))

    assert [p["id"] for p in resp["providers"]] == ["qwen", "deepseek-cloud"]
    assert _by_id(resp)["deepseek-cloud"]["base_url"] == "https://api.deepseek.com/v2"


async def test_post_provider_chinese_name_keeps_its_own_id(web_env, monkeypatch):
    """中文名字也能派生 id（汉字原样保留，不必转拼音）。"""
    _stub_rebuild(monkeypatch)
    resp = await srv.set_provider(_custom_entry(name="智谱 开放平台"))
    assert resp["provider_id"] == "智谱-开放平台"


async def test_post_provider_second_tier_does_not_steal_active(web_env, monkeypatch):
    """只有"第一档"才自动上位；第二档配好之后当前启用保持不变（切档要显式点）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())

    resp = await srv.set_provider(
        _custom_entry(name="Moonshot", base_url="https://api.moonshot.cn/v1", models=["kimi-k2"])
    )

    assert resp["active"] == "deepseek-cloud"
    assert _by_id(resp)["moonshot"]["is_active"] is False


async def test_post_provider_cannot_create_second_builtin_tier(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.set_provider(
            srv.ProviderEntryIn(id="dashscope2", name="另一个千问", kind="builtin")
        )
    assert ei.value.status_code == 400
    assert "内置" in ei.value.detail


async def test_post_provider_cannot_turn_builtin_tier_into_custom(web_env, monkeypatch):
    """内置那档只能改它自己的密钥，不能就地改成自定义档（要换家就新建一档）。"""
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.set_provider(_custom_entry(id="qwen", name="千问"))
    assert ei.value.status_code == 400
    assert "不能改成自定义" in ei.value.detail


async def test_post_provider_updates_builtin_tier_key(web_env, monkeypatch):
    """内置千问档存自己的密钥是允许的（留空则走 .env），且不回显明文。"""
    _stub_rebuild(monkeypatch)
    resp = await srv.set_provider(
        srv.ProviderEntryIn(
            id="qwen", name="千问（DashScope）", kind="builtin", api_key="sk-qwen-12345678"
        )
    )
    qwen = _by_id(resp)["qwen"]
    assert qwen["has_key"] is True
    assert "sk-qwen-12345678" not in json.dumps(resp, ensure_ascii=False)


# --------------------------------------------------------------- 切档 / 删档


async def test_activate_switches_current_tier_and_rebuilds(web_env, monkeypatch):
    """切档立刻生效：模型入口跟着换，运行时配置也换成新档的模型。"""
    captured = _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())

    resp = await srv.activate_provider("qwen")
    assert resp["active"] == "qwen"
    assert _by_id(resp)["qwen"]["is_active"] is True
    assert len(captured["builds"]) == 2
    # 切回内置档：运行时不再用自定义端点
    assert web_state.provider.source == "default"

    resp = await srv.activate_provider("deepseek-cloud")
    assert resp["active"] == "deepseek-cloud"
    assert web_state.settings.model == "deepseek-chat"


async def test_activate_unknown_tier_is_404(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.activate_provider("nope")
    assert ei.value.status_code == 404


async def test_activate_unconfigured_tier_is_400(web_env, monkeypatch):
    """没配好的档不能启用（哪怕 id 存在、就藏在文件里）。"""
    _stub_rebuild(monkeypatch)
    _write_raw_book(
        providers=[
            {"id": "qwen", "name": "千问（DashScope）", "kind": "builtin"},
            {
                "id": "half-done",
                "name": "填了一半",
                "kind": "custom",
                "base_url": "",
                "api_key": "sk-x",
                "models": ["m1"],
            },
        ]
    )
    with pytest.raises(HTTPException) as ei:
        await srv.activate_provider("half-done")
    assert ei.value.status_code == 400
    assert "不能启用" in ei.value.detail


async def test_delete_tier_falls_back_to_first_available(web_env, monkeypatch):
    """删掉的正好是当前启用档时，自动落到第一个可用档（不会留下"没有当前档"的空档）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())  # 这一档当前启用

    resp = await srv.delete_provider("deepseek-cloud")

    assert resp["ok"] is True
    assert [p["id"] for p in resp["providers"]] == ["qwen"]
    assert resp["active"] == "qwen"
    assert _by_id(resp)["qwen"]["is_active"] is True
    assert web_runtime.provider_book().find("deepseek-cloud") is None


async def test_delete_non_active_tier_keeps_active(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())
    resp = await srv.set_provider(
        _custom_entry(name="Moonshot", base_url="https://api.moonshot.cn/v1", models=["kimi-k2"])
    )
    assert resp["active"] == "deepseek-cloud"

    resp = await srv.delete_provider("moonshot")
    assert resp["active"] == "deepseek-cloud"
    assert [p["id"] for p in resp["providers"]] == ["qwen", "deepseek-cloud"]


async def test_delete_builtin_tier_is_400(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.delete_provider("qwen")
    assert ei.value.status_code == 400
    assert "不能删除" in ei.value.detail


async def test_delete_unknown_tier_is_404(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await srv.delete_provider("nope")
    assert ei.value.status_code == 404


async def test_writes_are_rejected_while_answering(web_env, monkeypatch):
    """正在回答中不许换供应商：三个写接口同一道闸（重建会把在跑那一轮的循环换掉）。"""
    _stub_rebuild(monkeypatch)
    monkeypatch.setattr(web_state.loop, "busy", True)
    for call in (
        lambda: srv.set_provider(_custom_entry()),
        lambda: srv.activate_provider("qwen"),
        lambda: srv.delete_provider("qwen"),
    ):
        with pytest.raises(HTTPException) as ei:
            await call()
        assert ei.value.status_code == 409


# --------------------------------------------------------------- GET /api/models


async def test_models_lists_every_model_of_active_tier_without_filtering(web_env, monkeypatch):
    """当前档的模型**全部**列出来：不再按"能不能当大脑"过滤（只填看图模型也算数）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry(models=["tts-1", "gpt-4o"], name="OpenAI 兼容端点"))

    resp = await srv.list_models()

    assert [m["id"] for m in resp["models"]] == ["tts-1", "gpt-4o"]
    assert resp["default"] == "tts-1"  # 清单第一个就是默认（用户没另选过）
    by = {m["id"]: m for m in resp["models"]}
    assert by["gpt-4o"]["supports_vision"] is True
    assert by["tts-1"]["supports_vision"] is False


async def test_models_uses_model_catalog_metadata_from_request(web_env, monkeypatch):
    """用户在目录里填的显示名 / 上下文窗口优先于推断层（填了就以填的为准）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(
        _custom_entry(
            models=[
                {
                    "id": "my-model",
                    "label": "我的模型",
                    "context_window": 128000,
                    "max_output": 8192,
                    "inputs": ["text", "image"],
                }
            ]
        )
    )

    resp = await srv.list_models()
    item = resp["models"][0]
    assert item["label"] == "我的模型"
    assert item["context_window"] == 128000
    assert item["supports_vision"] is True  # 明确填了 image


async def test_models_switches_back_to_builtin_catalog(web_env, monkeypatch):
    """切回内置档：模型入口回到内置实测清单（两档绝不混排）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())
    await srv.activate_provider("qwen")

    resp = await srv.list_models()
    assert [m["id"] for m in resp["models"]] == [m.id for m in web_runtime.MAIN_MODELS]
    assert resp["default"] == web_runtime.DEFAULT_MAIN_MODEL_ID


async def test_models_and_builtin_items_share_same_shape(web_env, monkeypatch):
    """两档同一张皮：字段集合一致，前端只认一套形状。"""
    builtin = await srv.list_models()

    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())
    custom = await srv.list_models()

    assert set(builtin["models"][0]) == set(custom["models"][0])


# --------------------------------------------------------------- 老接口不受影响


async def test_legacy_config_endpoint_still_works_after_provider_tier(web_env, monkeypatch):
    """老接口（POST /api/config）行为不变：切主脑 / 思考档照旧，写 v1 形状文件。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())
    await srv.activate_provider("qwen")

    resp = await srv.set_config(srv.ConfigRequest(model="qwen3.8-max", reasoning_effort="high"))

    assert resp["model"] == "qwen3.8-max"
    assert web_state.settings.model == "qwen3.8-max"
    # 老前端读的是 v1 形状：文件被 v1 覆盖后 load_provider_config 仍读得出来
    assert srv.load_provider_config().source in ("default", "custom")


async def test_tools_endpoint_reports_active_models(web_env, monkeypatch):
    """/api/tools 的 models 仍是真实生效的模型名（老前端 model-tag 靠它）。"""
    _stub_rebuild(monkeypatch)
    await srv.set_provider(_custom_entry())

    models = (await srv.list_tools())["models"]
    assert models["chat"] == "deepseek-chat"


# --------------------------------------------------------------- 版本号与只读


async def test_get_providers_reports_revision_and_writability(web_env):
    """设置页要两样东西：版本号（冲突检测）与只读标志（只读部署禁用写按钮）。"""
    resp = await srv.get_providers()
    assert isinstance(resp["revision"], int)
    assert resp["read_only"] is False


async def test_stale_revision_is_rejected_with_conflict_message(web_env, monkeypatch):
    """拿旧版号来保存 = 卡片打开期间被别处改过：409 + DSH 的原话。"""
    _stub_rebuild(monkeypatch)
    first = await srv.get_providers()
    await srv.set_provider(_custom_entry(revision=first["revision"]))

    with pytest.raises(HTTPException) as err:
        await srv.set_provider(_custom_entry(name="另一档", revision=first["revision"]))
    assert err.value.status_code == 409
    assert "其他地方改动" in err.value.detail


async def test_successful_save_bumps_revision(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    before = await srv.get_providers()
    await srv.set_provider(_custom_entry(revision=before["revision"]))
    after = await srv.get_providers()
    assert after["revision"] > before["revision"]


async def test_delete_and_activate_reject_stale_revision(web_env, monkeypatch):
    """删档 / 切档也要版本校验：拿旧版号来改，一律 409。"""
    _stub_rebuild(monkeypatch)
    first = await srv.get_providers()
    await srv.set_provider(_custom_entry(revision=first["revision"]))
    stale = first["revision"]

    with pytest.raises(HTTPException) as err:
        await srv.activate_provider("qwen", revision=stale)
    assert err.value.status_code == 409

    with pytest.raises(HTTPException) as err2:
        await srv.delete_provider("qwen", revision=stale)
    assert err2.value.status_code == 409


async def test_read_only_flag_follows_file_writability(web_env, monkeypatch):
    """配置文件不可写时，GET /api/providers 必须报 read_only=true（前端据此禁用写按钮）。"""
    monkeypatch.setattr(web_runtime, "provider_file_writable", lambda: False)
    resp = await srv.get_providers()
    assert resp["read_only"] is True

# ------------------------------------------------- 能力 / 思考档的保存期校验（2026-10-29）


async def test_context_window_must_be_positive(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as err:
        await srv.set_provider(_custom_entry(models=[{"id": "m1", "context_window": -1}]))
    assert err.value.status_code == 400
    assert "上下文窗口必须是正数" in err.value.detail


async def test_max_output_must_be_positive(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as err:
        await srv.set_provider(_custom_entry(models=[{"id": "m1", "max_output": "abc"}]))
    assert err.value.status_code == 400
    assert "最大输出 token 数必须是正数" in err.value.detail


async def test_blank_reasoning_value_is_rejected_but_missing_is_filled(web_env, monkeypatch):
    """显式清空 → 拦；压根没写 → 默认与档位同名（照 DSH 的 defaultValue）。"""
    _stub_rebuild(monkeypatch)
    with pytest.raises(HTTPException) as err:
        await srv.set_provider(
            _custom_entry(
                models=[{"id": "m1", "reasoning_levels": ["high"], "reasoning_values": {"high": "  "}}],
            )
        )
    assert "Off 以外的档位都需要填写 API 参数值" in err.value.detail

    resp = await srv.set_provider(
        _custom_entry(revision=(await srv.get_providers())["revision"],
                      models=[{"id": "m1", "reasoning_levels": ["low", "high"]}])
    )
    assert resp["ok"] is True
    deep = _by_id(await srv.get_providers())["deepseek-cloud"]
    assert deep["models"][0]["reasoning_values"] == {"low": "low", "high": "high"}

async def test_models_flags_missing_selection(web_env, monkeypatch):
    """上次选的模型不在清单里 → selected_missing=true 并给出原 id（前端据此提示 + 切换）。"""
    from dataclasses import replace as dc_replace

    _stub_rebuild(monkeypatch)
    rev = (await srv.get_providers())["revision"]
    await srv.set_provider(_custom_entry(revision=rev, models=["m1", "m2"]))
    book = web_runtime.provider_book()
    pid = book.active
    web_runtime.persist_book(
        dc_replace(book, last_models={**book.last_models, pid: "gone-model"}),
    )
    resp = await srv.list_models()
    assert resp["selected_missing"] is True
    assert resp["replaced_from"] == "gone-model"
    assert resp["default"] == "m1"


async def test_models_reports_no_missing_when_present(web_env, monkeypatch):
    _stub_rebuild(monkeypatch)
    rev = (await srv.get_providers())["revision"]
    await srv.set_provider(_custom_entry(revision=rev, models=["m1", "m2"]))
    resp = await srv.list_models()
    assert resp["selected_missing"] is False
    assert resp["replaced_from"] == ""