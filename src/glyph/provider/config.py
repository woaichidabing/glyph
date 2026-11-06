"""模型供应商配置的数据模型：两档来源。

- ``default``（默认 / 内置）：千问 DashScope 的 OpenAI 兼容端点，地址固定，
  用户只需在设置里填一把千问 API Key；
- ``custom``（自定义）：任意 OpenAI 兼容端点，用户自带 base_url、API Key，
  以及从上游 ``GET /models`` 拉取或手动添加的模型清单。

本模块只做数据结构与容错解析，不读盘、不联网。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "SOURCE_DEFAULT",
    "SOURCE_CUSTOM",
    "SOURCES",
    "CustomProvider",
    "RoleModels",
    "ProviderConfig",
    "looks_like_default_key",
    "normalize_models",
]

SOURCE_DEFAULT = "default"
SOURCE_CUSTOM = "custom"
SOURCES = (SOURCE_DEFAULT, SOURCE_CUSTOM)


# 千问官方档的钥匙形状底线：以 sk- 开头、后面至少 4 个字符。
# 刻意**不做**严格格式校验（钥匙的长度与字符集会变，死抠只会把好钥匙挡在门外），
# 只要能挡住"明摆着不是钥匙"的输入即可 —— 2026-10-12 真实事故：设置页里残留一个字符
# "k"，它按"UI 设置优先于 .env"的规则把好钥匙整把盖掉，之后每一次对话都是 401，
# 而界面上完全看不出原因。
DEFAULT_KEY_PREFIX = "sk-"
DEFAULT_KEY_MIN_BODY = 4


def looks_like_default_key(key: str) -> bool:
    """这把千问官方档的钥匙看着像不像一把钥匙（只挡"明摆着不是"，不做严格校验）。"""
    text = key.strip()
    if not text.startswith(DEFAULT_KEY_PREFIX):
        return False
    return len(text[len(DEFAULT_KEY_PREFIX) :]) >= DEFAULT_KEY_MIN_BODY


def _clean_str(value: object) -> str:
    """字符串去首尾空白；非字符串一律当空（粘贴进来的 None / 数字不炸解析）。"""
    return value.strip() if isinstance(value, str) else ""


def normalize_models(items: Iterable[object] | object) -> tuple[str, ...]:
    """模型 id 清单：只收字符串、去首尾空白、去空、去重并保持首次出现顺序。

    传进字符串（而不是列表）时返回空元组——模型清单必须是列表，单个字符串不算。
    """
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        return ()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class RoleModels:
    """逐能力指定的模型绑定（两档通用）：空串 = 未指定，走自动适配。

    - ``vision``：看图（拍照认字 / 描述图片）用的模型；
    - ``grade``：批改（评分 / 纠错 / 复核 / 写作分析）用的模型；
    - 对话（主脑）刻意不在这里：它已有单一事实源——默认档是主脑选择
      （``Settings.model``），自定义档是 :class:`CustomProvider.selected_model`，
      这里再存一份必然漂移。

    解析规则（落地点在 :mod:`glyph.provider.apply`，web 层回显与它保持同一套）：
    - 自定义档：绑定必须在清单内，否则视为失效、落回自动——看图取清单里第一个
      支持视觉的，批改跟对话主脑同款；
    - 默认档：显式指定才覆盖，未指定沿用 .env / 内置推荐（env 调试后门照旧可用）。
    """

    vision: str = ""
    grade: str = ""

    def to_dict(self) -> dict:
        return {"vision": self.vision, "grade": self.grade}

    @classmethod
    def from_dict(cls, data: object) -> RoleModels:
        if not isinstance(data, dict):
            return cls()
        return cls(
            vision=_clean_str(data.get("vision")),
            grade=_clean_str(data.get("grade")),
        )


@dataclass(frozen=True)
class CustomProvider:
    """自定义 OpenAI 兼容供应商：地址、钥匙、模型清单与当前选中的主脑。"""

    base_url: str = ""
    api_key: str = ""
    #: 协议（openai-chat / openai-responses / anthropic-messages）；老配置缺省按 responses
    api: str = "openai-responses"
    models: tuple[str, ...] = ()
    selected_model: str = ""
    #: 当前选中模型「声明支持」的思考档（v2 模型目录里那几档）；空 = 不发思考参数
    reasoning_levels: tuple[str, ...] = ()
    #: 档位 → 发给厂商的参数值（留空表示同名）；发送端优先用它
    reasoning_values: dict[str, str] = field(default_factory=dict)
    #: 选中模型的标称上下文窗口（0 = 用户没填 → 装配层按兜底 256K 算）
    selected_context_window: int = 0
    #: 选中模型的最大输出（0 = 没填 → 按兜底 32K 算）
    selected_max_output: int = 0
    #: 选中模型自己能不能看图（目录里"输入类型"勾了图片）；能看就直接把图交给它
    main_model_can_see_image: bool = False

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api": self.api,
            "models": list(self.models),
            "selected_model": self.selected_model,
            "reasoning_levels": list(self.reasoning_levels),
            "reasoning_values": dict(self.reasoning_values),
            "selected_context_window": self.selected_context_window,
            "selected_max_output": self.selected_max_output,
            "main_model_can_see_image": self.main_model_can_see_image,
        }

    @classmethod
    def from_dict(cls, data: object) -> CustomProvider:
        if not isinstance(data, dict):
            return cls()
        return cls(
            base_url=_clean_str(data.get("base_url")),
            api_key=_clean_str(data.get("api_key")),
            api=_clean_str(data.get("api")) or "openai-responses",
            models=normalize_models(data.get("models")),
            selected_model=_clean_str(data.get("selected_model")),
            reasoning_levels=tuple(
                item.strip()
                for item in (data.get("reasoning_levels") or [])
                if isinstance(item, str) and item.strip()
            ),
            reasoning_values=dict(data.get("reasoning_values") or {})
            if isinstance(data.get("reasoning_values"), dict)
            else {},
            selected_context_window=int(data.get("selected_context_window") or 0),
            selected_max_output=int(data.get("selected_max_output") or 0),
            main_model_can_see_image=bool(data.get("main_model_can_see_image")),
        )


@dataclass(frozen=True)
class ProviderConfig:
    """供应商两档选择 + 各自要持久化的字段（role_models 两档共用）。"""

    source: str = SOURCE_DEFAULT
    default_api_key: str = ""
    custom: CustomProvider = field(default_factory=CustomProvider)
    role_models: RoleModels = field(default_factory=RoleModels)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "default_api_key": self.default_api_key,
            "custom": self.custom.to_dict(),
            "role_models": self.role_models.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> ProviderConfig:
        """从任意 JSON 数据容错解析：结构不对 / 字段缺失 / 来源非法一律安全回退。"""
        if not isinstance(data, dict):
            return cls()
        source = data.get("source")
        if source not in SOURCES:
            source = SOURCE_DEFAULT
        return cls(
            source=source,
            default_api_key=_clean_str(data.get("default_api_key")),
            custom=CustomProvider.from_dict(data.get("custom")),
            role_models=RoleModels.from_dict(data.get("role_models")),
        )
