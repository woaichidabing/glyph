"""零依赖 frontmatter 解析 / 序列化测试。"""

from glyph.memory.frontmatter import render_frontmatter, split_frontmatter


def test_roundtrip_simple_fields() -> None:
    fields = {
        "type": "preference",
        "count": "3",
        "title": "讲解用中文",
        "key": "abcdef012345",
    }

    rendered = render_frontmatter(fields)
    parsed, body = split_frontmatter(rendered + "\n正文在这里\n")

    assert parsed == fields
    assert body == "正文在这里"


def test_parse_tolerant_skips_bad_lines() -> None:
    text = "---\ntype: preference\n这一行没有冒号是手写垃圾\ncount: 2\n---\nbody\n"

    parsed, body = split_frontmatter(text)

    assert parsed["type"] == "preference"
    assert parsed["count"] == "2"
    assert "这一行" not in parsed
    assert body == "body"


def test_parse_without_fence_returns_empty() -> None:
    parsed, body = split_frontmatter("普通正文\n第二行")

    assert parsed == {}
    assert body == "普通正文\n第二行"


def test_render_orders_keys_deterministically() -> None:
    out = render_frontmatter({"b": "2", "a": "1"})

    lines = out.strip().split("\n")
    assert lines[0] == "---"
    assert lines[1] == "a: 1"
    assert lines[2] == "b: 2"
    assert lines[3] == "---"
