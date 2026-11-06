"""归一化与内容寻址键测试。"""

from glyph.memory.slug import compute_key, normalize_fragment
from glyph.memory.types import MemoryType


def test_normalize_collapses_whitespace_lowercases_and_strips_edge_punct() -> None:
    assert normalize_fragment("  I   Goes!\n") == "i goes"
    assert normalize_fragment("It are...") == "it are"


def test_normalize_nfkc_full_width() -> None:
    # 全角字母归一后与半角同键
    assert normalize_fragment("Ｈｅｌｌｏ") == "hello"


def test_compute_key_deterministic_12_hex() -> None:
    key1 = compute_key(MemoryType.PREFERENCE, title="讲解用中文")
    key2 = compute_key(MemoryType.PREFERENCE, title="讲解用中文")

    assert key1 == key2
    assert len(key1) == 12
    int(key1, 16)  # 纯十六进制


def test_compute_key_differs_by_type_and_title() -> None:
    assert compute_key(MemoryType.PREFERENCE, title="x") != compute_key(
        MemoryType.FEEDBACK, title="x"
    )
    assert compute_key(MemoryType.PREFERENCE, title="x") != compute_key(
        MemoryType.PREFERENCE, title="y"
    )


def test_mistake_key_uses_original_and_correction() -> None:
    base = compute_key(MemoryType.MISTAKE, original="I goes", correction="I went")
    # title 不参与 mistake 的键
    with_title = compute_key(
        MemoryType.MISTAKE,
        title="随便什么",
        original="I goes",
        correction="I went",
    )
    changed_correction = compute_key(MemoryType.MISTAKE, original="I goes", correction="I go")

    assert base == with_title
    assert base != changed_correction


def test_compute_key_normalizes_before_hashing() -> None:
    # 空白 / 大小写差异不产生新键
    assert compute_key(MemoryType.PREFERENCE, title="I goes") == compute_key(
        MemoryType.PREFERENCE, title="  i   GOES  "
    )


# ---------- mistake 三级退化（P0：不同错题不得互相覆盖） ----------


def test_mistake_without_identity_fields_falls_back_to_content() -> None:
    """拿不到原文 / 改法时，用整条正文做身份 → 不同的错不会撞成一个键。"""
    first = compute_key(MemoryType.MISTAKE, title="时态", content="I go to school yesterday")
    second = compute_key(MemoryType.MISTAKE, title="时态", content="He like play basketball")

    assert first != second
    # 同一条正文重复给 → 仍是同一个键（去重语义保住）
    assert first == compute_key(
        MemoryType.MISTAKE, title="另一个钩子", content="I go to school yesterday"
    )


def test_mistake_half_given_uses_the_given_half() -> None:
    """只给一半时不能拿空串凑身份。"""
    only_original = compute_key(MemoryType.MISTAKE, original="I go")
    only_correction = compute_key(MemoryType.MISTAKE, correction="I went")

    assert only_original != only_correction
    # 给了空串等同于没给
    assert only_original == compute_key(MemoryType.MISTAKE, original="I go", correction="")


def test_mistake_without_anything_is_stable_and_distinct() -> None:
    """什么都不给（历史调用路径）也不能退化成全项目同一个键。"""
    assert compute_key(MemoryType.MISTAKE, title="a") != compute_key(MemoryType.MISTAKE, title="b")
