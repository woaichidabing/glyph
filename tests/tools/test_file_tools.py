"""read_file 工具测试：只能读上传目录里的 txt/md，越界 / 错类型 / 穿越一律拒绝。"""

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.file_tools import ReadFileTool


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "uploads"
    d.mkdir()
    return d


def _tool(root, **kw):
    return ReadFileTool([root], **kw)


async def test_read_txt_returns_content(root) -> None:
    f = root / "a.txt"
    f.write_text("I am study English every day", encoding="utf-8")
    out = await _tool(root).run({"path": str(f)})
    assert out["content"] == "I am study English every day"
    assert out["chars"] == len("I am study English every day")
    assert out["truncated"] is False


async def test_read_markdown(root) -> None:
    f = root / "note.md"
    f.write_text("# title\nhello", encoding="utf-8")
    out = await _tool(root).run({"path": str(f)})
    assert "hello" in out["content"]


async def test_missing_path_arg_is_error(root) -> None:
    with pytest.raises(ToolError):
        await _tool(root).run({})


async def test_nonexistent_file_is_error(root) -> None:
    with pytest.raises(ToolError):
        await _tool(root).run({"path": str(root / "nope.txt")})


async def test_wrong_suffix_rejected(root) -> None:
    f = root / "a.py"
    f.write_text("print(1)", encoding="utf-8")
    with pytest.raises(ToolError):
        await _tool(root).run({"path": str(f)})


async def test_path_outside_roots_rejected(root, tmp_path) -> None:
    other = tmp_path / "secret.txt"
    other.write_text("secret", encoding="utf-8")
    with pytest.raises(ToolError):
        await _tool(root).run({"path": str(other)})


async def test_relative_traversal_rejected(root, tmp_path) -> None:
    # root/../outside.txt 解析后越出上传目录，必须拒绝
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    traversal = str(root / ".." / "outside.txt")
    with pytest.raises(ToolError):
        await _tool(root).run({"path": traversal})


async def test_truncates_long_content(root) -> None:
    f = root / "big.txt"
    f.write_text("a" * 50, encoding="utf-8")
    out = await ReadFileTool([root], max_chars=10).run({"path": str(f)})
    assert out["chars"] == 10
    assert out["truncated"] is True


async def test_gbk_encoded_file_decodes(root) -> None:
    f = root / "g.txt"
    f.write_bytes("你好".encode("gbk"))
    out = await _tool(root).run({"path": str(f)})
    assert "你好" in out["content"]
