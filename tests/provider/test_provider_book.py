"""供应商 v2（多档 + 当前启用）的纯逻辑口径。

三条要守住的：

1. **没配好就不算存在**：缺地址 / 缺密钥 / 空清单的档不进 valid()，也不会成为当前档；
2. **主模型由用户选**：不再要求“清单里有能当主脑的模型”，只填一个看图模型也合法；
3. **v1 迁移不丢东西**：老的 provider.json 读进来后，自定义端点与钥匙都还在。
"""

import pytest

from glyph.provider.book import (
    ProviderBook,
    ProviderEntry,
    ModelEntry,
    book_from_dict,
    book_to_dict,
    entry_from_dict,
    model_can_see_image,
    normalize_models,
    to_legacy,
    validate_entry,
)


def _custom(pid="relay", *, key="sk-1", url="https://relay.example/v1", models=("m1", "m2")):
    return ProviderEntry(
        id=pid,
        name="自建中转",
        kind="custom",
        api="openai-chat",
        base_url=url,
        api_key=key,
        models=tuple(ModelEntry(id=m) for m in models),
    )


def test_empty_document_has_only_the_builtin_entry():
    """空文件 = 只剩内置千问那一档（永远可用；模型来自内置目录）。"""
    book = book_from_dict({})
    assert [p.id for p in book.providers] == ["qwen"]
    assert book.current().id == "qwen"
    assert [p.id for p in book.valid()] == ["qwen"]


def test_parse_multi_provider_document():
    book = book_from_dict({
        "version": 2,
        "active": "relay",
        "providers": {
            "qwen": {"name": "千问", "kind": "builtin", "models": [{"id": "qwen3.8-max"}]},
            "relay": {
                "name": "自建中转",
                "kind": "custom",
                "base_url": "https://relay.example/v1/",
                "api_key": "sk-1",
                "models": [{"id": "deepseek-v4-pro", "label": "V4 Pro", "context_window": "1,000,000"}],
            },
        },
        "last_models": {"relay": "deepseek-v4-pro"},
    })
    assert book.active == "relay"
    assert [p.id for p in book.providers] == ["qwen", "relay"]
    relay = book.find("relay")
    assert relay is not None
    assert relay.base_url == "https://relay.example/v1"
    assert relay.models[0].context_window == 1000000
    assert book.last_models["relay"] == "deepseek-v4-pro"


def test_unconfigured_provider_is_invisible_and_never_active():
    """缺密钥的自建档：不进 valid()、不会是当前档；进不去的档不许当 active。"""
    book = book_from_dict({
        "version": 2,
        "active": "broken",
        "providers": {
            "broken": {"kind": "custom", "base_url": "https://x/v1", "models": ["m1"]},
            "relay": {"kind": "custom", "base_url": "https://y/v1", "api_key": "k", "models": ["m1"]},
        },
    })
    assert [p.id for p in book.valid()] == ["relay"]
    assert book.current() is not None and book.current().id == "relay"


def test_vision_only_provider_is_still_valid():
    """只填一个看图模型也合法：主模型由用户自己选，不在这里筛。"""
    entry = ProviderEntry(
        id="vl",
        kind="custom",
        base_url="https://vl/v1",
        api_key="k",
        models=(ModelEntry(id="ocr-vl", inputs=("text", "image")),),
    )
    validate_entry(entry)
    assert model_can_see_image(entry.models[0]) is True


@pytest.mark.parametrize(
    "entry, message",
    [
        (_custom(url=""), "API 地址"),
        (_custom(key=""), "API 密钥"),
        (_custom(models=()), "模型清单"),
    ],
)
def test_validate_entry_refuses_incomplete(entry, message):
    with pytest.raises(ValueError) as err:
        validate_entry(entry)
    assert message in str(err.value)


def test_models_are_deduped_and_label_backfilled():
    models = normalize_models(["a", {"id": "a"}, {"id": "b"}, {"label": "无名"}, ""])
    assert [m.id for m in models] == ["a", "b"]
    assert models[0].label == "a"


def test_roles_pointing_outside_catalog_are_dropped():
    entry = entry_from_dict({
        "id": "relay",
        "kind": "custom",
        "base_url": "https://x/v1",
        "api_key": "k",
        "models": [{"id": "m1"}],
        "roles": {"grade": "ghost", "vision": "m1", "unknown": "m1"},
    })
    assert entry is not None
    assert entry.roles == {"vision": "m1"}
    assert entry.role_model("grade") is None
    assert entry.role_model("vision").id == "m1"


def test_v1_custom_document_migrates_and_keeps_key():
    book = book_from_dict({
        "source": "custom",
        "default_api_key": "sk-qwen",
        "custom": {
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
            "models": ["deepseek-v4-pro", "deepseek-v4.1-flash"],
            "selected_model": "deepseek-v4-pro",
        },
        "role_models": {"vision": "deepseek-v4-pro", "grade": ""},
    })
    assert book.active == "custom"
    assert book.find("qwen").api_key == "sk-qwen"
    relay = book.find("custom")
    assert relay is not None and relay.api_key == "sk-relay"
    assert tuple(m.id for m in relay.models) == ("deepseek-v4-pro", "deepseek-v4.1-flash")
    assert relay.roles == {"vision": "deepseek-v4-pro"}
    assert book.last_models["custom"] == "deepseek-v4-pro"


def test_v1_default_document_stays_on_builtin():
    book = book_from_dict({"source": "default", "default_api_key": "sk-qwen"})
    assert book.active == "qwen"
    assert book.find("qwen").api_key == "sk-qwen"
    assert book.find("custom") is None


def test_to_legacy_maps_current_provider_for_runtime():
    book = book_from_dict({
        "version": 2,
        "active": "relay",
        "providers": {
            "relay": {
                "kind": "custom",
                "base_url": "https://relay.example/v1",
                "api_key": "sk-1",
                "models": [{"id": "m1"}, {"id": "m2"}],
                "roles": {"grade": "m2", "vision": "m1"},
            }
        },
        "last_models": {"relay": "m2"},
    })
    legacy = to_legacy(book)
    assert legacy.source == "custom"
    assert legacy.custom.base_url == "https://relay.example/v1"
    assert legacy.custom.selected_model == "m2"
    assert legacy.role_models.grade == "m2"
    assert legacy.role_models.vision == "m1"


def test_round_trip_keeps_everything_that_matters():
    book = ProviderBook(active="relay", providers=(_custom(),), last_models={"relay": "m1"})
    # settle=False：序列化剥掉明文后，此刻判不准"这档能不能用"，
    # 落定由调用方补水之后再做（与 store.load_provider_book 一致）
    again = book_from_dict(book_to_dict(book), settle=False)
    assert again.active == "relay"
    # 序列化永远不带明文：只留引用名
    assert again.find("relay").api_key == ""
    assert book_to_dict(book)["providers"][0]["api_key"] == ""
    assert tuple(m.id for m in again.find("relay").models) == ("m1", "m2")
    assert again.last_models["relay"] == "m1"


def test_last_efforts_round_trip():
    """每档上次选的思考档要能存回来（重启后恢复）。"""
    book = book_from_dict({
        "version": 2,
        "active": "relay",
        "providers": {"relay": {"kind": "custom", "base_url": "https://x/v1", "api_key": "k", "models": ["m1"]}},
        "last_efforts": {"relay": "high", "qwen": "low"},
    })
    assert book.last_efforts == {"relay": "high", "qwen": "low"}
    again = book_from_dict(book_to_dict(book))
    assert again.last_efforts == {"relay": "high", "qwen": "low"}

def test_legacy_save_keeps_model_metadata():
    # 老接口只交一串 id：已存模型的上下文 / 思考档 / 参数值不能被抹成默认。
    from glyph.provider.config import CustomProvider, ProviderConfig
    from glyph.provider.book import apply_legacy

    book = book_from_dict({
        "version": 2,
        "active": "relay",
        "providers": {
            "relay": {
                "kind": "custom",
                "base_url": "https://x/v1",
                "api_key": "k",
                "models": [
                    {"id": "m1", "label": "M1", "context_window": 131072,
                     "max_output": 8192, "inputs": ["text", "image"],
                     "reasoning_levels": ["low", "high"], "reasoning_values": {"high": "high"}},
                ],
            }
        },
    })
    legacy = ProviderConfig(
        source="custom",
        custom=CustomProvider(base_url="https://x/v1", api_key="k", models=("m1",), selected_model="m1"),
    )
    saved = apply_legacy(book, legacy)
    entry = saved.find("relay")
    model = entry.models[0]
    assert model.context_window == 131072
    assert model.reasoning_levels == ("low", "high")
    assert model.reasoning_values == {"high": "high"}

def test_hydrate_migrates_plaintext_key_into_credential_store(tmp_path):
    """老数据里的明文钥匙：迁到凭据文件，配置里只留 ref。"""
    from glyph.credentials import CredentialStore
    from glyph.provider.book import default_credential_ref, hydrate_credentials

    store = CredentialStore(tmp_path / "credentials.json")
    book = book_from_dict({
        "version": 2,
        "active": "relay",
        "providers": {
            "relay": {"kind": "custom", "base_url": "https://x/v1", "api_key": "sk-plain", "models": ["m1"]},
        },
    })
    hydrated = hydrate_credentials(book, store)
    entry = hydrated.find("relay")
    assert entry.api_key_env == default_credential_ref("relay") == "RELAY_API_KEY"
    assert store.resolve("RELAY_API_KEY") == "sk-plain"
    # 运行时形状里仍有值（发请求要用），但配置里存的是 ref
    assert entry.api_key == "sk-plain"
    from dataclasses import replace

    # 落盘用的字典里不该出现明文（api_key 字段留空，凭证只认 ref）
    disk = book_to_dict(replace(hydrated, providers=tuple(replace(e, api_key="") for e in hydrated.providers)))
    assert "sk-plain" not in str(disk)


def test_hydrate_is_idempotent_and_resolves_refs(tmp_path):
    """再跑一次不会重复写；已存 ref 的档能从凭据库里解析回值。"""
    from glyph.credentials import CredentialStore
    from glyph.provider.book import hydrate_credentials

    store = CredentialStore(tmp_path / "credentials.json")
    store.set("CUSTOM_KEY", "sk-from-store")
    book = book_from_dict({
        "version": 2,
        "active": "c",
        "providers": {"c": {"kind": "custom", "base_url": "https://x/v1", "api_key_env": "CUSTOM_KEY", "models": ["m1"]}},
    })
    first = hydrate_credentials(book, store)
    second = hydrate_credentials(first, store)
    assert second.find("c").api_key == "sk-from-store"
    assert second.find("c").api_key_env == "CUSTOM_KEY"


def test_hydrate_leaves_key_empty_when_unresolvable(tmp_path):
    from glyph.credentials import CredentialStore
    from glyph.provider.book import hydrate_credentials

    store = CredentialStore(tmp_path / "credentials.json")
    book = book_from_dict({
        "version": 2,
        "active": "c",
        "providers": {"c": {"kind": "custom", "base_url": "https://x/v1", "api_key_env": "GONE_KEY", "models": ["m1"]}},
    })
    out = hydrate_credentials(book, store)
    assert out.find("c").api_key == ""