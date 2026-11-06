"""控制字符清洗与按字节安全截断测试。"""

from glyph.memory.sanitize import clip_bytes, sanitize_text


def test_sanitize_normalizes_newlines() -> None:
    assert sanitize_text("a\r\nb\rc") == "a\nb\nc"


def test_sanitize_removes_control_chars() -> None:
    assert sanitize_text("a\x00b\x7fc") == "abc"
    # \n \t 保留
    assert sanitize_text("a\tb\nc") == "a\tb\nc"


def test_sanitize_strips_ansi_sequences() -> None:
    assert sanitize_text("\x1b[31mred\x1b[0m text") == "red text"


def test_clip_bytes_ascii() -> None:
    text, truncated = clip_bytes("abcdef", 4)

    assert text == "abcd"
    assert truncated is True


def test_clip_bytes_never_halves_multibyte_char() -> None:
    # "中文" 每字 3 字节；预算 4 字节只能保住一个字，不许切出半个字
    text, truncated = clip_bytes("中文", 4)

    assert text == "中"
    assert truncated is True
    text.encode("utf-8")  # 结果仍是合法 UTF-8


def test_clip_bytes_within_budget_is_unchanged() -> None:
    text, truncated = clip_bytes("中文", 6)

    assert text == "中文"
    assert truncated is False
