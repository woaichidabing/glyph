"""供应商配置的"请求合并 + 完整性校验"纯逻辑。

保存时不是整体覆盖、而是逐字段"给了才改、不给保留"，并在落配置前把不完整的
自定义档（缺地址 / 缺 Key / 选中了清单外模型）挡下来。校验失败抛 ValueError，
由 web 层转成 400。
"""

import pytest

from glyph.provider.config import (
    SOURCE_CUSTOM,
    SOURCE_DEFAULT,
    CustomProvider,
    ProviderConfig,
    RoleModels,
)
from glyph.provider.merge import merge_provider, repair_role_models


def test_default_updates_key_and_keeps_custom_untouched():
    current = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k", models=("gpt-4o",)),
    )
    out = merge_provider(current, source=SOURCE_DEFAULT, default_api_key="sk-qwen")
    assert out.source == SOURCE_DEFAULT
    assert out.default_api_key == "sk-qwen"
    # 切回默认档不抹掉自定义信息，方便用户再切回去
    assert out.custom == current.custom


def test_default_blank_key_clears_ui_key_falling_back_to_env():
    out = merge_provider(
        ProviderConfig(source=SOURCE_DEFAULT, default_api_key="old"),
        source=SOURCE_DEFAULT,
        default_api_key="",
    )
    assert out.default_api_key == ""


def test_custom_first_time_full_config_accepted():
    out = merge_provider(
        ProviderConfig(),
        source=SOURCE_CUSTOM,
        custom={
            "base_url": "https://api.example.com/v1/",
            "api_key": " sk-custom ",
            "models": ["gpt-4o", "gpt-4o", "claude"],
            "selected_model": "gpt-4o",
        },
    )
    assert out.source == SOURCE_CUSTOM
    assert out.custom.base_url == "https://api.example.com/v1"
    assert out.custom.api_key == "sk-custom"
    assert out.custom.models == ("gpt-4o", "claude")
    assert out.custom.selected_model == "gpt-4o"


def test_custom_missing_url_rejected():
    with pytest.raises(ValueError):
        merge_provider(
            ProviderConfig(),
            source=SOURCE_CUSTOM,
            custom={"api_key": "k", "models": ["gpt-4o"]},
        )


def test_custom_missing_key_rejected():
    with pytest.raises(ValueError):
        merge_provider(
            ProviderConfig(),
            source=SOURCE_CUSTOM,
            custom={"base_url": "https://x/v1", "models": ["gpt-4o"]},
        )


def test_custom_partial_update_keeps_existing_url_and_key():
    current = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k1", models=("gpt-4o",)),
    )
    out = merge_provider(
        current,
        source=SOURCE_CUSTOM,
        custom={"models": ["gpt-4o", "gpt-4o-mini"]},
    )
    assert out.custom.base_url == "https://x/v1"
    assert out.custom.api_key == "k1"
    assert out.custom.models == ("gpt-4o", "gpt-4o-mini")


def test_custom_selection_must_be_in_catalog():
    current = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k1", models=("gpt-4o",)),
    )
    with pytest.raises(ValueError):
        merge_provider(current, source=SOURCE_CUSTOM, custom={"selected_model": "claude"})


def test_custom_empty_selection_is_allowed():
    current = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k1", models=("gpt-4o",)),
    )
    out = merge_provider(current, source=SOURCE_CUSTOM, custom={"selected_model": ""})
    assert out.custom.selected_model == ""


def test_illegal_source_rejected():
    with pytest.raises(ValueError):
        merge_provider(ProviderConfig(), source="mystery")


def test_switching_to_default_preserves_default_key():
    current = ProviderConfig(source=SOURCE_DEFAULT, default_api_key="sk-qwen")
    out = merge_provider(current, source=SOURCE_DEFAULT)
    assert out.default_api_key == "sk-qwen"


# ---------- 钥匙形状底线（2026-10-12 真实事故的入口拦截） ----------


def test_default_key_that_cannot_be_a_key_is_rejected():
    """设置页里残留一个字符 "k"，按"UI 优先于 .env"盖掉好钥匙后，每一次对话都是 401，
    界面上却完全看不出原因。明摆着不像钥匙的输入，保存时就该挡下来。"""
    with pytest.raises(ValueError) as exc:
        merge_provider(
            ProviderConfig(source=SOURCE_DEFAULT),
            source=SOURCE_DEFAULT,
            default_api_key="k",
        )
    assert "钥匙" in str(exc.value)


def test_default_key_without_sk_prefix_is_rejected():
    with pytest.raises(ValueError):
        merge_provider(
            ProviderConfig(source=SOURCE_DEFAULT),
            source=SOURCE_DEFAULT,
            default_api_key="1234567890abcdef",
        )


def test_default_key_of_normal_shape_is_accepted():
    key = "sk-" + "0" * 32
    out = merge_provider(
        ProviderConfig(source=SOURCE_DEFAULT),
        source=SOURCE_DEFAULT,
        default_api_key=key,
    )
    assert out.default_api_key == key


# ---------- 逐能力绑定（role_models）：合并 + 失效修复 ----------


def test_role_models_patch_sets_both_slots():
    out = merge_provider(
        ProviderConfig(source=SOURCE_DEFAULT),
        source=SOURCE_DEFAULT,
        role_models={"vision": "qwen3-vl-plus", "grade": "glm-5.2"},
    )
    assert out.role_models == RoleModels(vision="qwen3-vl-plus", grade="glm-5.2")


def test_role_models_partial_patch_keeps_other_slot():
    """只改批改，不许把看图的绑定顺手清掉（patch 用「出现过的键才改」语义）。"""
    current = ProviderConfig(role_models=RoleModels(vision="qv", grade="gd"))
    out = merge_provider(current, source=SOURCE_DEFAULT, role_models={"grade": "gn"})
    assert out.role_models == RoleModels(vision="qv", grade="gn")


def test_role_models_none_value_clears_binding_to_auto():
    out = merge_provider(
        ProviderConfig(source=SOURCE_DEFAULT, role_models=RoleModels(vision="qv")),
        source=SOURCE_DEFAULT,
        role_models={"vision": None},
    )
    assert out.role_models.vision == ""
    assert out.role_models.grade == ""


def test_role_models_absent_keeps_current():
    current = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k", models=("qv",)),
        role_models=RoleModels(vision="qv", grade="gd"),
    )
    out = merge_provider(current, source=SOURCE_CUSTOM, custom={"models": ["qv"]})
    assert out.role_models == RoleModels(vision="qv", grade="gd")


def test_role_models_values_are_trimmed():
    out = merge_provider(
        ProviderConfig(),
        source=SOURCE_DEFAULT,
        role_models={"vision": "  qwen3-vl-plus  ", "grade": "   "},
    )
    assert out.role_models == RoleModels(vision="qwen3-vl-plus", grade="")


def test_repair_keeps_bindings_inside_catalog():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k", models=("qv", "qg")),
        role_models=RoleModels(vision="qv", grade="qg"),
    )
    out, fallbacks = repair_role_models(cfg)
    assert out is cfg
    assert fallbacks == ()


def test_repair_clears_dangling_bindings_and_reports():
    """端点换了 / 清单变了，绑定指向已删模型 → 置回自动，并报告回退的槽位。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://x/v1", api_key="k", models=("qv",)),
        role_models=RoleModels(vision="gone-v", grade="gone-g"),
    )
    out, fallbacks = repair_role_models(cfg)
    assert out.role_models == RoleModels()
    assert fallbacks == ("vision", "grade")


def test_repair_default_tier_is_noop():
    """默认档没有清单可对照（型号存在与否只有真机调用才知道），不做修复。"""
    cfg = ProviderConfig(source=SOURCE_DEFAULT, role_models=RoleModels(vision="mystery-model"))
    out, fallbacks = repair_role_models(cfg)
    assert out.role_models.vision == "mystery-model"
    assert fallbacks == ()
