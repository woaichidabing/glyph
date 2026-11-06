"""模型目录里的「思考档」：只收词表值、去重、按词表顺序。"""

from glyph.provider.book import (
    ModelEntry,
    entry_from_dict,
    normalize_reasoning,
    normalize_reasoning_values,
)


def test_normalize_reasoning_keeps_vocabulary_order():
    assert normalize_reasoning(["high", "low", "xhigh"]) == ("low", "high", "xhigh")


def test_normalize_reasoning_drops_unknown_and_dedupes():
    assert normalize_reasoning(["low", "low", "ultra", ""]) == ("low",)


def test_normalize_reasoning_accepts_single_string_and_junk():
    assert normalize_reasoning("max") == ("max",)
    assert normalize_reasoning(None) == ()
    assert normalize_reasoning(42) == ()


def test_entry_round_trips_reasoning_levels():
    entry = entry_from_dict({
        "id": "relay",
        "kind": "custom",
        "base_url": "https://x/v1",
        "api_key": "k",
        "models": [{"id": "m1", "reasoning_levels": ["low", "xhigh"]}],
    })
    assert entry is not None
    assert entry.models[0].reasoning_levels == ("low", "xhigh")
    again = entry_from_dict(entry.to_dict())
    assert again.models[0].reasoning_levels == ("low", "xhigh")


def test_model_without_levels_stays_empty():
    assert ModelEntry(id="m1").reasoning_levels == ()


def test_reasoning_values_keep_only_vocabulary_and_drop_blanks():
    """参数值：只收词表里的档位；空值等于“同档位名”，不存。"""
    assert normalize_reasoning_values({"low": "low", "xhigh": "ultra", "max": "  "}) == {
        "low": "low",
        "xhigh": "ultra",
    }
    assert normalize_reasoning_values({"ultra": "x"}) == {}
    assert normalize_reasoning_values(None) == {}


def test_entry_round_trips_reasoning_values():
    entry = entry_from_dict({
        "id": "relay",
        "kind": "custom",
        "base_url": "https://x/v1",
        "api_key": "k",
        "models": [{"id": "m1", "reasoning_levels": ["high"], "reasoning_values": {"high": "high-effort"}}],
    })
    assert entry.models[0].reasoning_values == {"high": "high-effort"}
    again = entry_from_dict(entry.to_dict())
    assert again.models[0].reasoning_values == {"high": "high-effort"}
