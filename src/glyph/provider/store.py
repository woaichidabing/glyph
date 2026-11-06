"""供应商配置的本地读写。

- 文件不存在 / JSON 损坏 / 结构异常：一律回内置默认，绝不抛异常阻断启动
  （配置永不成为异常源，和 settings 加载同一原则）；
- 保存走"写临时文件再 os.replace"的原子替换，避免写到一半被读到半截 JSON；
- 显式 UTF-8（路径与配置都可能含中文环境）。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from .book import (
    KIND_BUILTIN,
    ProviderBook,
    ProviderEntry,
    book_from_dict,
    book_to_disk_dict,
)
from .config import SOURCE_CUSTOM, ProviderConfig
from .paths import provider_config_path

__all__ = [
    "load_provider_config",
    "save_provider_config",
    "load_provider_book",
    "save_provider_book",
]


def _sanitize(cfg: ProviderConfig) -> ProviderConfig:
    """修复存量脏数据：自定义档的选中模型必须在清单里，否则清空（运行时回落清单第一个）。

    2026-10-18 真机踩到：切来源时把内置型号（qwen3.8-27b）写进了 selected_model，
    上游是 DeepSeek 端点，请求必然「模型不存在」，界面还显示一个选择器里没有的名字。
    """
    if cfg.source != SOURCE_CUSTOM:
        return cfg
    selected = cfg.custom.selected_model
    if selected and selected not in cfg.custom.models:
        return replace(cfg, custom=replace(cfg.custom, selected_model=""))
    return cfg


def load_provider_config(path: Path | None = None) -> ProviderConfig:
    """读取供应商配置；任何异常都回退到内置默认档。"""
    path = path if path is not None else provider_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return ProviderConfig()
    try:
        return _sanitize(ProviderConfig.from_dict(data))
    except Exception:  # 结构再怪也不炸启动：兜底回默认档
        return ProviderConfig()


def save_provider_config(cfg: ProviderConfig, path: Path | None = None) -> None:
    """原子写入供应商配置（自动建父目录）。"""
    path = path if path is not None else provider_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ---------------------------------------------------------------- v2（多档）
def load_provider_book(path: Path | None = None) -> ProviderBook:
    """读取整本配置（v2）；文件不存在 / 坏 / 是 v1 → 一律给出一本能用的书。

    v1 的迁移在这里发生：老文件的字段被 translate 成 v2 形状，**但本函数不写盘**
    （读路径不许有副作用），要落盘由调用方在启动时就绪时显式调 save_provider_book。
    """
    path = path if path is not None else provider_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        # 连文件都没有：只剩内置千问那一档
        return ProviderBook(providers=(_builtin_entry(),), active="qwen")
    try:
        # settle=False：文件里没有明文钥匙（只存引用名），这一刻判不准哪档能用；
        # 由调用方补水（hydrate_credentials）之后再 settle_active。
        book = book_from_dict(data, settle=False)
    except Exception:  # 结构再怪也不炸启动
        return ProviderBook(providers=(_builtin_entry(),), active="qwen")
    return book


def save_provider_book(book: ProviderBook, path: Path | None = None) -> None:
    """原子写入整本配置（v2）。

    **任何落盘都先过凭据迁移**：有明文钥匙的档会被写进凭据文件、配置里只留引用名。
    这样"配置文件里永远没有明文钥匙"是一条结构保证，而不是靠每个调用方自觉。
    """
    from ..credentials import CredentialStore
    from .book import hydrate_credentials

    book = hydrate_credentials(book, CredentialStore())
    path = path if path is not None else provider_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(book_to_disk_dict(book), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _builtin_entry() -> ProviderEntry:
    """内置千问那一档的空壳（模型与地址由运行时目录提供）。"""
    return ProviderEntry(
        id="qwen",
        name="千问（DashScope）",
        kind=KIND_BUILTIN,
        api="openai-responses",
    )
