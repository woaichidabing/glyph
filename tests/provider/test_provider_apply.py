"""把持久化的供应商选择叠加到 .env/环境变量 Settings 上。

- 默认档：base_url 固定千问官方兼容端点；设置里填了千问 key 才覆盖 .env 的 key；
- 自定义档：base_url / key / 主脑模型整体换成用户的 OpenAI 兼容端点；
- 自定义档没填完整（缺地址或缺 key）时安全退回默认，绝不把运行时搞成空 key。
"""

from glyph.config import (
    DEFAULT_BASE_URL,
    DEFAULT_COMPACT_MODEL,
    DEFAULT_GRADE_FALLBACK_MODELS,
    DEFAULT_MAIN_FALLBACK_MODELS,
    DEFAULT_POLISH_MODEL,
    DEFAULT_VISION_MODEL,
    Settings,
)
from glyph.provider.apply import apply_provider
from glyph.provider.config import (
    SOURCE_CUSTOM,
    SOURCE_DEFAULT,
    CustomProvider,
    ProviderConfig,
    RoleModels,
)


def _settings(**over) -> Settings:
    base = dict(
        api_key="env-key",
        base_url=DEFAULT_BASE_URL,
        model="deepseek-v4.1-flash",
        max_tokens=8192,
        max_tokens_upgrade=65536,
        grade_model="deepseek-v4-pro",
        stream_idle_timeout=90.0,
        tool_timeout=120.0,
        reasoning_effort="medium",
    )
    base.update(over)
    return Settings(**base)


def test_default_without_saved_key_keeps_env_key_and_pins_official_url():
    out = apply_provider(_settings(), ProviderConfig(source=SOURCE_DEFAULT))
    assert out.api_key == "env-key"
    assert out.base_url == DEFAULT_BASE_URL
    assert out.model == "deepseek-v4.1-flash"


def test_default_with_saved_key_overrides_env_key():
    cfg = ProviderConfig(source=SOURCE_DEFAULT, default_api_key="sk-ui-dashscope")
    out = apply_provider(_settings(), cfg)
    assert out.api_key == "sk-ui-dashscope"
    assert out.base_url == DEFAULT_BASE_URL


def test_default_pins_url_back_to_official_even_if_env_points_elsewhere():
    out = apply_provider(
        _settings(base_url="https://some-gateway.example/v1"),
        ProviderConfig(source=SOURCE_DEFAULT),
    )
    assert out.base_url == DEFAULT_BASE_URL


def test_custom_switches_url_key_and_selected_model():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1/",
            api_key="sk-custom",
            models=("gpt-4o", "claude-3-5-sonnet"),
            selected_model="claude-3-5-sonnet",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.base_url == "https://api.example.com/v1"  # 尾斜杠被去掉
    assert out.api_key == "sk-custom"
    assert out.model == "claude-3-5-sonnet"


def test_custom_without_selection_uses_first_model():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-4o-mini", "gpt-4o"),
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.model == "gpt-4o-mini"


def test_incomplete_custom_without_url_falls_back_safely():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(api_key="sk-custom", models=("gpt-4o",)),
    )
    out = apply_provider(_settings(), cfg)
    assert out.base_url == DEFAULT_BASE_URL
    assert out.api_key == "env-key"  # 没有被空值清掉


def test_incomplete_custom_without_key_falls_back_safely():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(base_url="https://api.example.com/v1", models=("gpt-4o",)),
    )
    out = apply_provider(_settings(), cfg)
    assert out.base_url == DEFAULT_BASE_URL
    assert out.api_key == "env-key"


def test_apply_preserves_unrelated_settings():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-4o",),
            selected_model="gpt-4o",
        ),
    )
    out = apply_provider(_settings(context_mode="max", speaking_style="witty"), cfg)
    assert out.reasoning_effort == "medium"
    assert out.context_mode == "max"
    assert out.speaking_style == "witty"
    # #64：自定义档下 grade_model 不再保留内置名，映射到选中模型（上游只有它）
    assert out.grade_model == "gpt-4o"


def test_custom_maps_text_submodels_to_selected_and_clears_builtin_fallbacks():
    """自定义档：批改/润色/摘要都用选中模型；内置兜底链在上游不存在，必须清空。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-4o-mini",),
            selected_model="gpt-4o-mini",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.model == "gpt-4o-mini"
    assert out.grade_model == "gpt-4o-mini"
    assert out.polish_model == "gpt-4o-mini"
    assert out.compact_model == "gpt-4o-mini"
    assert out.grade_fallback_models == ()
    assert out.main_fallback_models == ()


def test_custom_without_vision_model_leaves_vision_empty():
    """清单全是纯文本模型：没有看图模型，置空（装配层据此不注册视觉工具、前端禁图片入口）。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-3.5-turbo", "deepseek-chat"),
            selected_model="deepseek-chat",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.vision_model == ""


def test_custom_picks_first_vision_model_from_catalog():
    """清单里有视觉模型：vision_model 取第一个能看图的（即便选中的主脑是纯文本）。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-3.5-turbo", "gpt-4o"),
            selected_model="gpt-3.5-turbo",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.vision_model == "gpt-4o"
    # 批改仍跟选中的主脑，不被视觉模型顶替
    assert out.grade_model == "gpt-3.5-turbo"


def test_custom_vision_selected_when_main_model_itself_sees_images():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("qwen3-vl-plus",),
            selected_model="qwen3-vl-plus",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.vision_model == "qwen3-vl-plus"


def test_default_key_that_cannot_be_a_key_falls_back_to_env():
    """存量坏钥匙的最后一道防线：加载时发现它根本不像钥匙，就忽略它、用 .env 里那把。

    2026-10-12 真实事故：设置页残留一个字符 "k"，后端每次对话都 401。保存端点会拦新的
    坏输入，但已经写进 provider.json 的那个必须在加载时被识破，产品才能自己恢复可用。
    """
    cfg = ProviderConfig(source=SOURCE_DEFAULT, default_api_key="k")
    out = apply_provider(_settings(), cfg)
    assert out.api_key == "env-key"
    assert out.base_url == DEFAULT_BASE_URL


def test_default_does_not_remap_submodels():
    """默认（千问）档：批改/润色/看图/兜底链全部保持内置，不被自定义逻辑污染。"""
    out = apply_provider(_settings(), ProviderConfig(source=SOURCE_DEFAULT))
    assert out.grade_model == "deepseek-v4-pro"
    assert out.polish_model == DEFAULT_POLISH_MODEL
    assert out.vision_model == DEFAULT_VISION_MODEL
    assert out.compact_model == DEFAULT_COMPACT_MODEL
    assert out.grade_fallback_models == DEFAULT_GRADE_FALLBACK_MODELS
    assert out.main_fallback_models == DEFAULT_MAIN_FALLBACK_MODELS


# ---------- 逐能力绑定（role_models，2026-10-13）：两档通用 ----------


def test_custom_grade_binding_overrides_chat_model():
    """自定义档：批改显式指定优先，不再一律跟对话同款。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-4o", "deepseek-v3"),
            selected_model="gpt-4o",
        ),
        role_models=RoleModels(grade="deepseek-v3"),
    )
    out = apply_provider(_settings(), cfg)
    assert out.grade_model == "deepseek-v3"
    assert out.model == "gpt-4o"  # 主脑不受影响
    assert out.polish_model == "gpt-4o"  # 润色不在本次范围，仍跟主脑


def test_custom_grade_binding_not_in_catalog_falls_back_to_chat():
    """落盘绑定被手改 / 半旧（不在清单内）：按未指定处理，跟对话同款，绝不跑死名字。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-4o",),
            selected_model="gpt-4o",
        ),
        role_models=RoleModels(grade="gone-model"),
    )
    out = apply_provider(_settings(), cfg)
    assert out.grade_model == "gpt-4o"


def test_custom_vision_binding_overrides_first_vision_pick():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-4o", "claude-3-5-sonnet"),
            selected_model="gpt-4o",
        ),
        role_models=RoleModels(vision="claude-3-5-sonnet"),
    )
    out = apply_provider(_settings(), cfg)
    # 绑定优先于"清单第一个能看图的"自动选择（这里 claude 不被认为能看图，仍尊重指定）
    assert out.vision_model == "claude-3-5-sonnet"


def test_custom_no_bindings_keeps_auto_behavior():
    """两槽都没指定：行为与加 role_models 之前完全一致（批改跟主脑、看图取第一个视觉）。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-custom",
            models=("gpt-3.5-turbo", "gpt-4o"),
            selected_model="gpt-3.5-turbo",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.grade_model == "gpt-3.5-turbo"
    assert out.vision_model == "gpt-4o"


def test_default_grade_binding_overrides_env_and_builtin():
    """默认档：显式指定优先于 .env / 内置推荐（UI 保存 > env 的既有次序不变）。"""
    out = apply_provider(
        _settings(grade_model="env-grade"),
        ProviderConfig(source=SOURCE_DEFAULT, role_models=RoleModels(grade="glm-5.2")),
    )
    assert out.grade_model == "glm-5.2"
    # 没指定的槽位沿用 .env / 内置推荐，行为与从前一致
    assert out.vision_model == DEFAULT_VISION_MODEL
    assert out.polish_model == DEFAULT_POLISH_MODEL


def test_default_vision_binding_overrides_env_and_builtin():
    out = apply_provider(
        _settings(vision_model="env-vision"),
        ProviderConfig(source=SOURCE_DEFAULT, role_models=RoleModels(vision="qwen3-vl-plus")),
    )
    assert out.vision_model == "qwen3-vl-plus"
    assert out.grade_model == "deepseek-v4-pro"


def test_custom_provider_carries_protocol_and_reasoning_support():
    """自定义档的协议与「当前模型声明了档位」都要落到 Settings 上。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://x/v1",
            api_key="k",
            api="openai-chat",
            models=("m1",),
            selected_model="m1",
            reasoning_levels=("low", "medium"),
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.api_protocol == "openai-chat"
    assert out.reasoning_supported is True


def test_builtin_provider_stays_on_responses():
    out = apply_provider(_settings(), ProviderConfig())
    assert out.api_protocol == "openai-responses"
    assert out.reasoning_supported is True

def test_custom_provider_carries_reasoning_values():
    """档位 → 参数值 也要落到 Settings（发送端优先用它）。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://x/v1",
            api_key="k",
            models=("m1",),
            selected_model="m1",
            reasoning_levels=("high",),
            reasoning_values={"high": "high-effort"},
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.reasoning_values == {"high": "high-effort"}

def test_max_output_falls_back_to_32k_when_not_filled():
    """没填最大输出 → 请求按 32K 走（产品所有者 2026-10-29 定）。"""
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://x/v1",
            api_key="k",
            models=("m1",),
            selected_model="m1",
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.max_tokens == 32_768
    assert out.max_tokens_upgrade == 32_768


def test_filled_max_output_is_used_as_is():
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://x/v1",
            api_key="k",
            models=("m1",),
            selected_model="m1",
            selected_max_output=65_536,
            selected_context_window=1_000_000,
        ),
    )
    out = apply_provider(_settings(), cfg)
    assert out.max_tokens == 65_536
    assert out.model_context_window == 1_000_000

def test_capability_falls_back_to_first_model_before_any_selection():
    """刚加的档还没在胶囊里选过模型：能力按清单第一个算，不能全空。

    2026-11-01 真机：新加的档声明了图片输入，但因为"没选过"，
    main_model_can_see_image 是 False → 界面把拖图入口关掉且不给理由。
    """
    from glyph.provider.book import ModelEntry, ProviderBook, ProviderEntry, to_legacy

    entry = ProviderEntry(
        id="fresh",
        name="fresh",
        kind="custom",
        api="openai-chat",
        base_url="https://example.com/v1",
        api_key_env="FRESH_API_KEY",
        api_key="k",
        models=(
            ModelEntry(id="m1", label="m1", inputs=("text", "image"), context_window=32000),
        ),
    )
    book = ProviderBook(active="fresh", providers=(entry,), last_models={}, revision=1)
    cfg = to_legacy(book)
    assert cfg.custom.main_model_can_see_image is True
    assert cfg.custom.selected_context_window == 32000
