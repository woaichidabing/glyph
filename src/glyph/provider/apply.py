"""把持久化的供应商选择叠加到从 .env / 环境变量加载的 Settings 上。

加载顺序：``load_settings()``（.env / 环境变量）打底，再用本函数叠加用户在
设置页保存、落盘到 provider.json 的选择——UI 保存的优先级高于 .env。

- 默认档：地址强制回到千问官方兼容端点（默认档就是千问，不让 base_url 飘）；
  只有用户在设置里填了千问 Key 才覆盖 .env 的 Key，没填就沿用 .env；批改 / 看图
  子模型在「高级」里显式指定了才覆盖（指定优先于 .env），未指定沿用 .env / 内置
  推荐——env 与内置推荐共用一个兜底槽，没有界面指定时行为与从前完全一致；
  润色 / 兜底链等其余子模型保持内置。
- 自定义档且地址 / Key 都填完整：整体换成用户的端点。上游只有用户自己的模型，内置的
  批改 / 润色 / 摘要子模型名（deepseek-v4-pro / qwen3.7-plus 等）在第三方上游大概率
  不存在，故这些子任务映射到选中的主模型——批改在「高级」里显式指定的型号除外；
  内置兜底链（kimi / flash）同样会指向不存在的名字，一律清空；看图模型取「高级」里
  显式指定的（必须在清单内），未指定则取清单里第一个支持视觉的，没有则置空（装配层
  据此不注册视觉工具、前端禁用图片入口），而不是硬调一个纯文本模型把请求打爆。
- 自定义档没填完整（缺地址或缺 Key）：安全退回默认档逻辑，绝不把运行时
  搞成空地址 / 空 Key（保存端点也会拦，这里是最后一道防线）。

绑定过期（端点换了、清单变了）在保存时由 :func:`glyph.provider.merge.repair_role_models`
修复；本函数对自定义档再做一次防御性核对——落盘数据可能被手改 / 半旧，绑定不在清单内
就按未指定处理，绝不让一个死名字跑到上游。
"""

from __future__ import annotations

from dataclasses import replace

from ..config import DEFAULT_BASE_URL, Settings
from ..llm.models import DEFAULT_MAX_OUTPUT_TOKENS
from .capabilities import capabilities_for
from .config import SOURCE_CUSTOM, ProviderConfig, looks_like_default_key

__all__ = ["apply_provider"]


def apply_provider(settings: Settings, cfg: ProviderConfig) -> Settings:
    """返回叠加供应商选择后的新 Settings（frozen，不就地改）。"""
    custom = cfg.custom
    if cfg.source == SOURCE_CUSTOM and custom.base_url.strip() and custom.api_key.strip():
        changes: dict[str, object] = {
            "base_url": custom.base_url.strip().rstrip("/"),
            "api_key": custom.api_key.strip(),
            # 协议决定装配层走哪条通道（chat / responses / anthropic）
            "api_protocol": custom.api or "openai-responses",
            # 当前模型声明了思考档 → 请求里才允许带 reasoning_effort
            "reasoning_supported": bool(custom.reasoning_levels),
            "reasoning_values": dict(custom.reasoning_values or {}),
            "reasoning_levels": tuple(custom.reasoning_levels or ()),
            # 能力只认清单：用户填了窗口/输出就按填的，没填用兜底（不再按名字猜）
            "model_context_window": int(custom.selected_context_window or 0),
            # 主模型自己声明能看图吗（目录里"输入类型"勾了图片）
            "main_model_can_see_image": bool(custom.main_model_can_see_image),
            "max_tokens": int(custom.selected_max_output or DEFAULT_MAX_OUTPUT_TOKENS),
            "max_tokens_upgrade": int(custom.selected_max_output or DEFAULT_MAX_OUTPUT_TOKENS),
        }
        model = custom.selected_model.strip()
        if model not in custom.models:
            # 存量脏数据（切来源时把内置型号写进了 selected_model）回落清单第一个，
            # 绝不让一个上游不存在的型号跑上请求。
            model = custom.models[0] if custom.models else ""
        if model:
            changes["model"] = model
            # 上游只有用户自己的模型：润色 / L3 摘要统一用选中主模型，
            # 否则这些工具会拿内置名去第三方端点调"模型不存在"。
            changes["polish_model"] = model
            changes["compact_model"] = model
            # 内置兜底名在上游同样不存在，留着只会在主模型失败后再撞一次"模型不存在"。
            changes["grade_fallback_models"] = ()
            changes["main_fallback_models"] = ()
            # 批改：「高级」里显式指定的优先（必须在清单内，防御过期落盘），否则跟对话同款。
            grade_binding = cfg.role_models.grade
            changes["grade_model"] = grade_binding if grade_binding in custom.models else model
            # 看图：显式指定的优先（同样防御），否则清单里第一个支持视觉的；没有就置空软降级。
            vision_binding = cfg.role_models.vision
            changes["vision_model"] = (
                vision_binding
                if vision_binding in custom.models
                else next(
                    (m for m in custom.models if capabilities_for(m).supports_vision),
                    "",
                )
            )
        return replace(settings, **changes)

    changes: dict[str, object] = {"base_url": DEFAULT_BASE_URL, "api_protocol": "openai-responses"}
    saved_key = cfg.default_api_key.strip()
    # 最后一道防线：存量里那把"根本不像钥匙"的值直接忽略、回落到 .env。
    # 保存端点会拦新的坏输入（merge_provider），但已经写进 provider.json 的老数据
    # 必须在这里被识破，否则产品会一直 401 且界面上看不出原因（2026-10-12 真实事故）。
    if saved_key and looks_like_default_key(saved_key):
        changes["api_key"] = saved_key
    # 逐能力指定（两档通用）：默认档只有显式指定才覆盖，未指定沿用 .env / 内置推荐。
    vision_binding = cfg.role_models.vision
    if vision_binding:
        changes["vision_model"] = vision_binding
    grade_binding = cfg.role_models.grade
    if grade_binding:
        changes["grade_model"] = grade_binding
    return replace(settings, **changes)
