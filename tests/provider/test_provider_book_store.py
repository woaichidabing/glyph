"""v2 配置文件的读写：坏文件不炸、v1 迁移、写回不乱。"""

import json

from glyph.provider.book import ModelEntry, ProviderBook, ProviderEntry, book_to_dict
from glyph.provider.store import load_provider_book, save_provider_book


def test_missing_file_gives_builtin_only(tmp_path):
    book = load_provider_book(tmp_path / "provider.json")
    assert [p.id for p in book.providers] == ["qwen"]
    assert book.active == "qwen"


def test_broken_file_never_raises(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text("{ not json", encoding="utf-8")
    book = load_provider_book(path)
    assert book.current() is not None


def test_write_then_read_round_trip(tmp_path):
    path = tmp_path / "provider.json"
    book = ProviderBook(
        active="relay",
        providers=(
            ProviderEntry(id="qwen", kind="builtin"),
            ProviderEntry(
                id="relay",
                name="自建中转",
                kind="custom",
                base_url="https://relay.example/v1",
                api_key="sk-1",
                models=(ModelEntry(id="deepseek-v4-pro"),),
            ),
        ),
    )
    save_provider_book(book, path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    again = load_provider_book(path)
    assert again.active == "relay"
    # 2026-10-28 起：配置文件里不放明文钥匙，只放引用名（值在 ~/.markr/credentials.json）
    assert again.find("relay").api_key_env == "RELAY_API_KEY"
    assert again.find("relay").api_key == ""
    assert "sk-1" not in path.read_text(encoding="utf-8")


def test_v1_file_is_read_as_v2(tmp_path):
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({
        "source": "custom",
        "default_api_key": "sk-qwen",
        "custom": {
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
            "models": ["deepseek-v4-pro"],
            "selected_model": "deepseek-v4-pro",
        },
    }), encoding="utf-8")
    book = load_provider_book(path)
    assert book.active == "custom"
    assert book.find("custom").api_key == "sk-relay"
    assert book.find("qwen").api_key == "sk-qwen"


def test_written_document_is_stable_json(tmp_path):
    path = tmp_path / "provider.json"
    save_provider_book(ProviderBook(providers=(ProviderEntry(id="qwen", kind="builtin"),), active="qwen"), path)
    first = path.read_text(encoding="utf-8")
    save_provider_book(load_provider_book(path), path)
    assert path.read_text(encoding="utf-8") == first
    assert book_to_dict(load_provider_book(path))["active"] == "qwen"
