"""供应商配置的数据模型与本地存取（纯逻辑，不碰网络）。

覆盖：缺文件回默认、自定义档往返、模型清单去空白去重保序、坏 JSON / 非法字段
一律安全回默认（配置永不成为启动异常源）。
"""

import json

from glyph.provider.config import (
    SOURCE_CUSTOM,
    SOURCE_DEFAULT,
    CustomProvider,
    ProviderConfig,
    normalize_models,
)
from glyph.provider.store import load_provider_config, save_provider_config


def test_missing_file_returns_builtin_default(tmp_path):
    cfg = load_provider_config(tmp_path / "provider.json")
    assert cfg == ProviderConfig()
    assert cfg.source == SOURCE_DEFAULT
    assert cfg.custom == CustomProvider()


def test_custom_config_roundtrip(tmp_path):
    path = tmp_path / "provider.json"
    cfg = ProviderConfig(
        source=SOURCE_CUSTOM,
        custom=CustomProvider(
            base_url="https://api.example.com/v1",
            api_key="sk-secret",
            models=("gpt-4o", "claude-3-5-sonnet"),
            selected_model="claude-3-5-sonnet",
        ),
    )
    save_provider_config(cfg, path)
    assert load_provider_config(path) == cfg


def test_default_api_key_roundtrip(tmp_path):
    path = tmp_path / "provider.json"
    cfg = ProviderConfig(source=SOURCE_DEFAULT, default_api_key="sk-dashscope")
    save_provider_config(cfg, path)
    assert load_provider_config(path).default_api_key == "sk-dashscope"


def test_save_creates_parent_dirs_and_is_valid_utf8_json(tmp_path):
    path = tmp_path / "nested" / "deep" / "provider.json"
    save_provider_config(ProviderConfig(default_api_key="k"), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source"] == SOURCE_DEFAULT
    assert data["default_api_key"] == "k"


def test_normalize_models_trims_dedups_preserves_order():
    assert normalize_models([" gpt-4o ", "", "gpt-4o", "claude", None, 123, " claude "]) == (
        "gpt-4o",
        "claude",
    )


def test_corrupt_json_returns_default_without_raising(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text("{ not valid json", encoding="utf-8")
    assert load_provider_config(path) == ProviderConfig()


def test_illegal_source_falls_back_to_default(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({"source": "somewhere-else"}), encoding="utf-8")
    assert load_provider_config(path).source == SOURCE_DEFAULT


def test_custom_with_missing_fields_is_empty_not_crash(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({"source": SOURCE_CUSTOM}), encoding="utf-8")
    cfg = load_provider_config(path)
    assert cfg.source == SOURCE_CUSTOM
    assert cfg.custom.base_url == ""
    assert cfg.custom.models == ()


def test_custom_models_non_list_is_treated_as_empty(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text(
        json.dumps({"source": SOURCE_CUSTOM, "custom": {"models": "gpt-4o"}}),
        encoding="utf-8",
    )
    assert load_provider_config(path).custom.models == ()


def test_trims_surrounding_whitespace_on_load(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text(
        json.dumps(
            {
                "source": SOURCE_CUSTOM,
                "custom": {
                    "base_url": "  https://api.example.com/v1  ",
                    "api_key": "  sk-x  ",
                    "models": ["  gpt-4o "],
                    "selected_model": " gpt-4o",
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_provider_config(path)
    assert cfg.custom.base_url == "https://api.example.com/v1"
    assert cfg.custom.api_key == "sk-x"
    assert cfg.custom.models == ("gpt-4o",)
    assert cfg.custom.selected_model == "gpt-4o"
