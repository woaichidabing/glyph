"""模型供应商配置落盘位置：环境变量可覆盖，默认在 ~/.markr/provider.json。

与 memory / attempts / sessions 的 paths 测试同构：绝不写真实目录，
覆盖路径靠 GLYPH_PROVIDER_CONFIG 注入。
"""

from pathlib import Path

from glyph.provider.paths import PROVIDER_CONFIG_ENV, provider_config_path
from glyph.storage import data_root


def test_default_path_under_markr(monkeypatch):
    monkeypatch.delenv(PROVIDER_CONFIG_ENV, raising=False)
    assert provider_config_path() == data_root() / "provider.json"


def test_env_override_points_at_given_file(monkeypatch, tmp_path):
    target = tmp_path / "provider.json"
    monkeypatch.setenv(PROVIDER_CONFIG_ENV, str(target))
    assert provider_config_path() == target


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(PROVIDER_CONFIG_ENV, "   ")
    assert provider_config_path() == data_root() / "provider.json"
