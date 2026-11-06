"""尝试记录存储根目录与路径单点（T07，离线）。

与长期记忆的 ``~/.markr/memory`` 平级，尝试记录放 ``~/.markr/attempts``，
可用 ``GLYPH_ATTEMPTS_ROOT`` 覆盖（测试与便携部署都靠它）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from glyph.attempts.paths import attempt_path, attempts_root, is_valid_attempt_id, trash_dir
from glyph.storage import data_root


def test_默认根在markr用户目录(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLYPH_ATTEMPTS_ROOT", raising=False)
    assert attempts_root() == data_root() / "attempts"


def test_环境变量覆盖根(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "my_attempts"
    monkeypatch.setenv("GLYPH_ATTEMPTS_ROOT", str(custom))
    assert attempts_root() == custom


def test_条目文件名带前缀且落在根下(tmp_path: Path) -> None:
    p = attempt_path(tmp_path, "0123abcd4567")
    assert p.name == "attempt-0123abcd4567.md"
    assert p.parent == tmp_path


def test_回收站是根下隐藏子目录(tmp_path: Path) -> None:
    assert trash_dir(tmp_path) == tmp_path / ".trash"


@pytest.mark.parametrize(
    ("raw", "ok"),
    [
        ("0123456789ab", True),
        ("abcdefabcdef", True),
        # 长度不对 / 含非十六进制 / 带路径分隔符 —— 一律不认，防路径穿越
        ("0123456789a", False),
        ("0123456789abc", False),
        ("ghijklmnopqr", False),
        ("../../etc", False),
        ("0123456789ab/", False),
        ("", False),
    ],
)
def test_attempt_id格式校验(raw: str, ok: bool) -> None:
    assert is_valid_attempt_id(raw) is ok
