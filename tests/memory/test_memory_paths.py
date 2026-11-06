"""memory 路径单点测试。"""

from pathlib import Path

from glyph.memory.paths import entry_path, memory_root, trash_dir
from glyph.memory.types import MemoryType
from glyph.storage import data_root


def test_memory_root_default(monkeypatch):
    monkeypatch.delenv("GLYPH_MEMORY_ROOT", raising=False)

    assert memory_root() == data_root() / "memory"


def test_memory_root_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GLYPH_MEMORY_ROOT", str(tmp_path))

    assert memory_root() == tmp_path


def test_trash_dir_is_under_root(tmp_path):
    assert trash_dir(tmp_path) == tmp_path / ".trash"


def test_entry_path_shape(tmp_path):
    path = entry_path(tmp_path, MemoryType.MISTAKE, "abcdef012345")

    assert path == tmp_path / "mistake-abcdef012345.md"
