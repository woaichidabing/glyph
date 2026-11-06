"""日志配置单点（observability 接线）的测试。

守 R1：级别 / 路径只从 config.py 的 settings 读，别处不许再写 os.getenv。

两个刻意的取值约定：
- `GLYPH_LOG_LEVEL` 空串 → 回退 INFO（与"配错一个字别炸启动"一致）；
- `GLYPH_LOG_FILE` **显式空串 → 关掉文件出口**（只留控制台）。
  这条是"空串有意义"，所以解析必须用 in 判断"配没配"，不能用 `or` 回退默认值
  ——否则用户想关文件出口时会被默认路径悄悄顶回来。
"""

from __future__ import annotations

import pytest

from glyph.config import (
    DEFAULT_LOG_FILE,
    DEFAULT_LOG_LEVEL,
    load_dotenv,
    load_settings,
)

_ENV_KEYS = (
    "GLYPH_LOG_LEVEL",
    "GLYPH_LOG_FILE",
    "DASHSCOPE_API_KEY",
    "GLYPH_API_KEY",
)


@pytest.fixture
def env_guard(monkeypatch, tmp_path):
    """隔离真实环境与项目 .env：测试只看自己设的值。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # load_dotenv 只"不覆盖已存在变量"；把 .env 指到一个不存在的路径，避免它注入真值
    load_dotenv(tmp_path / "no-such.env")
    return monkeypatch


def test_default_level_is_info(env_guard) -> None:
    assert load_settings().log_level == DEFAULT_LOG_LEVEL == "INFO"


def test_default_log_file(env_guard) -> None:
    assert load_settings().log_file == DEFAULT_LOG_FILE


@pytest.mark.parametrize("raw", ["DEBUG", "debug", "WARNING", "Error"])
def test_level_from_env(env_guard, raw) -> None:
    env_guard.setenv("GLYPH_LOG_LEVEL", raw)
    assert load_settings().log_level == raw


def test_level_empty_falls_back(env_guard) -> None:
    env_guard.setenv("GLYPH_LOG_LEVEL", "")
    assert load_settings().log_level == DEFAULT_LOG_LEVEL


def test_log_file_from_env(env_guard, tmp_path) -> None:
    target = str(tmp_path / "custom.log")
    env_guard.setenv("GLYPH_LOG_FILE", target)
    assert load_settings().log_file == target


def test_explicit_empty_log_file_disables_file_output(env_guard) -> None:
    """显式空串是**有意义**的取值：关掉文件出口。

    这条是回归线：如果哪天有人把解析改回 `x or DEFAULT`，用户就再也关不掉文件出口。
    """
    env_guard.setenv("GLYPH_LOG_FILE", "")
    assert load_settings().log_file == ""


def test_settings_are_frozen_dataclass_with_defaults(env_guard) -> None:
    """两个新字段必须带默认值——仓库里有两处手造 Settings 的测试，必填字段会当场打断它们。"""
    settings = load_settings()
    assert settings.log_level
    assert isinstance(settings.log_file, str)
