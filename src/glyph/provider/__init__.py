"""模型供应商配置：默认千问 / 自定义 OpenAI 兼容两档，本地持久化。

公共入口：
- :class:`ProviderConfig` / :class:`CustomProvider` / :class:`RoleModels`：
  两档配置与逐能力绑定的数据模型；
- :func:`load_provider_config` / :func:`save_provider_config`：本地 JSON 读写；
- :func:`apply_provider`：把持久化选择叠加到从 .env 加载的 Settings；
- :func:`merge_provider` / :func:`repair_role_models`：保存请求的逐字段合并、
  完整性校验与失效绑定修复；
- :func:`fetch_upstream_models`：一键拉取自定义端点的上游模型清单；
- :func:`capabilities_for` / :func:`capabilities_payload`：推断自定义模型的
  能力位（上下文 / 工具 / 思考档 / 看图），内置实测表优先。
"""

from __future__ import annotations

from .apply import apply_provider
from .book import (
    ProviderBook,
    ProviderEntry,
    apply_legacy,
    book_from_dict,
    book_to_dict,
    to_legacy,
)
from .capabilities import (
    ModelCapabilities,
    capabilities_for,
    capabilities_payload,
    infer_brand,
    infer_capabilities,
)
from .config import (
    SOURCE_CUSTOM,
    SOURCE_DEFAULT,
    CustomProvider,
    ProviderConfig,
    RoleModels,
    normalize_models,
)
from .discover import ProviderDiscoverError, fetch_upstream_models
from .merge import merge_provider, repair_role_models
from .store import (
    load_provider_book,
    load_provider_config,
    save_provider_book,
    save_provider_config,
)

__all__ = [
    "ProviderBook",
    "ProviderEntry",
    "apply_legacy",
    "book_from_dict",
    "book_to_dict",
    "load_provider_book",
    "save_provider_book",
    "to_legacy",
    "SOURCE_CUSTOM",
    "SOURCE_DEFAULT",
    "CustomProvider",
    "ProviderConfig",
    "RoleModels",
    "normalize_models",
    "fetch_upstream_models",
    "ProviderDiscoverError",
    "ModelCapabilities",
    "capabilities_for",
    "capabilities_payload",
    "infer_brand",
    "infer_capabilities",
    "merge_provider",
    "repair_role_models",
    "load_provider_config",
    "save_provider_config",
    "apply_provider",
]
