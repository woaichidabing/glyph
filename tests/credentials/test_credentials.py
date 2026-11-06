"""凭据通道：写进去、读不出；解析顺序 store → env → .env。

守三条（与 DSH 的 credentials seam 一致）：
1. describe 只回"配没配 / 从哪来 / 能不能写"，**永不回值**；
2. resolve 的顺序固定，空串一律当没配；
3. 坏文件 / 目录不存在都不抛异常（配置永不成为异常源）。
"""

import json
import os

from glyph.credentials import CredentialStore


def test_set_then_resolve(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.set("QWEN_API_KEY", " sk-test ")
    assert store.resolve("QWEN_API_KEY") == "sk-test"
    assert store.describe("QWEN_API_KEY").configured is True
    assert store.describe("QWEN_API_KEY").source == "store"


def test_describe_never_returns_value(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.set("A_KEY", "secret-value")
    info = store.describe("A_KEY")
    assert "secret-value" not in repr(info)


def test_missing_ref_is_reported_not_raised(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    assert store.resolve("NOPE_KEY") is None
    assert store.describe("NOPE_KEY").configured is False
    assert store.describe("NOPE_KEY").source == "missing"


def test_environ_fallback(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.json")
    monkeypatch.setenv("ENV_ONLY_KEY", "from-env")
    assert store.resolve("ENV_ONLY_KEY") == "from-env"
    assert store.describe("ENV_ONLY_KEY").source == "env"


def test_store_wins_over_environ(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.json")
    store.set("BOTH_KEY", "from-store")
    monkeypatch.setenv("BOTH_KEY", "from-env")
    assert store.resolve("BOTH_KEY") == "from-store"


def test_blank_env_value_counts_as_unset(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.json")
    monkeypatch.setenv("BLANK_KEY", "   ")
    assert store.resolve("BLANK_KEY") is None


def test_unset_removes_and_keeps_file_valid(tmp_path):
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    store.set("K1", "v1")
    store.set("K2", "v2")
    store.unset("K1")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["refs"] == {"K2": "v2"}


def test_broken_file_is_treated_as_empty(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    store = CredentialStore(path)
    assert store.resolve("ANY") is None
    store.set("NEW", "v")  # 坏文件也能被重写成合法文件
    assert store.resolve("NEW") == "v"


def test_empty_value_rejected(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    try:
        store.set("K", "   ")
    except ValueError as err:
        assert "不能为空" in str(err)
    else:
        raise AssertionError("空值必须被拒")
