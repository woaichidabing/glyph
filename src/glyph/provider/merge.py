"""把一次"保存供应商"请求合并进当前配置并做完整性校验（纯逻辑，不碰 IO / 网络）。

语义是**逐字段合并**：请求里给了（非 None）的字段才覆盖，没给的保留现状——
前端可能只更新模型清单而不重填地址 / Key。自定义档在合并后必须地址、Key 齐全，
选中模型必须在清单内，否则抛 ValueError（web 层转 400）。

逐能力绑定（``role_models``）走"给字段才改"的同一套合并；但**失效绑定不挡保存**，
由 :func:`repair_role_models` 在合并后静默置回自动——绑定指向的模型被删是正常操作，
报错会把人卡在设置页，回退 + 告知（web 层把回退槽位带回响应）才是对的出口。
"""

from __future__ import annotations

from dataclasses import replace

from .config import (
    SOURCE_CUSTOM,
    SOURCE_DEFAULT,
    SOURCES,
    CustomProvider,
    ProviderConfig,
    RoleModels,
    looks_like_default_key,
    normalize_models,
)

__all__ = ["merge_provider", "repair_role_models"]


def _merge_custom(current: CustomProvider, patch: dict | None) -> CustomProvider:
    if patch is None:
        patch = {}

    base_url = current.base_url
    if patch.get("base_url") is not None:
        base_url = str(patch["base_url"]).strip().rstrip("/")

    api_key = current.api_key
    if patch.get("api_key") is not None:
        api_key = str(patch["api_key"]).strip()

    models = current.models
    if patch.get("models") is not None:
        raw = patch["models"]
        models = normalize_models(raw) if isinstance(raw, list) else current.models

    selected_model = current.selected_model
    if patch.get("selected_model") is not None:
        selected_model = str(patch["selected_model"]).strip()

    merged = CustomProvider(
        base_url=base_url,
        api_key=api_key,
        models=models,
        selected_model=selected_model,
    )
    if not merged.base_url:
        raise ValueError("自定义模型需要填写接口地址（Base URL）")
    if not merged.api_key:
        raise ValueError("自定义模型需要填写 API Key")
    if merged.selected_model and merged.selected_model not in merged.models:
        raise ValueError("选中的模型不在已添加的模型清单里")
    return merged


def _merge_role_models(current: RoleModels, patch: dict | None) -> RoleModels:
    """逐能力绑定合并：patch 里给的字段才改；显式 null / 非字符串 = 清除（回自动）。"""
    if patch is None:
        return current
    vision = current.vision
    grade = current.grade
    if "vision" in patch:
        raw = patch["vision"]
        vision = raw.strip() if isinstance(raw, str) else ""
    if "grade" in patch:
        raw = patch["grade"]
        grade = raw.strip() if isinstance(raw, str) else ""
    return RoleModels(vision=vision, grade=grade)


def merge_provider(
    current: ProviderConfig,
    *,
    source: str,
    default_api_key: str | None = None,
    custom: dict | None = None,
    role_models: dict | None = None,
) -> ProviderConfig:
    """把本次请求合并进 ``current``，返回新的 ProviderConfig；非法输入抛 ValueError。

    ``role_models`` 的 patch 用「出现过的键才改」语义（web 层以 exclude_unset 下发），
    所以用户只改批改不会把看图的绑定顺手清掉。
    """
    if source not in SOURCES:
        raise ValueError(f"不支持的模型来源：{source}")

    merged_role_models = _merge_role_models(current.role_models, role_models)

    if source == SOURCE_DEFAULT:
        key = current.default_api_key
        if default_api_key is not None:
            key = str(default_api_key).strip()
            # 空串的语义是"清掉设置里的钥匙、回落到 .env"，本来就合法，不校验；
            # 非空但明显不是钥匙（一个字符、没有 sk- 前缀…）一律挡下，别让它盖掉好钥匙。
            if key and not looks_like_default_key(key):
                raise ValueError(
                    "这把钥匙看起来不完整：千问钥匙以 sk- 开头、后面还有一长串字符。"
                    "请检查是不是漏粘贴了；想改用 .env 里的钥匙，就把这一栏清空再保存。"
                )
        # 切回默认档不抹掉自定义信息，用户再切回去时还在
        return ProviderConfig(
            source=SOURCE_DEFAULT,
            default_api_key=key,
            custom=current.custom,
            role_models=merged_role_models,
        )

    merged_custom = _merge_custom(current.custom, custom)
    return ProviderConfig(
        source=SOURCE_CUSTOM,
        default_api_key=current.default_api_key,
        custom=merged_custom,
        role_models=merged_role_models,
    )


def repair_role_models(cfg: ProviderConfig) -> tuple[ProviderConfig, tuple[str, ...]]:
    """自定义档保存后的引用修复：绑定指向的 id 不在模型清单里 → 置回自动。

    返回 ``(修复后的配置, 发生回退的能力名)``；没有回退时原样返回、空元组。
    默认档没有"清单"可对照（型号存在与否只有真机调用才知道），不做修复——
    指定的型号跑不通时由工具层报人话，内置推荐永远是退路。
    """
    if cfg.source != SOURCE_CUSTOM:
        return cfg, ()
    models = cfg.custom.models
    fallbacks: list[str] = []
    vision = cfg.role_models.vision
    if vision and vision not in models:
        vision = ""
        fallbacks.append("vision")
    grade = cfg.role_models.grade
    if grade and grade not in models:
        grade = ""
        fallbacks.append("grade")
    if not fallbacks:
        return cfg, ()
    return (
        replace(cfg, role_models=RoleModels(vision=vision, grade=grade)),
        tuple(fallbacks),
    )
