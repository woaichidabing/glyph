"""偏好设置接口：存得住、白名单挡住乱写。"""
import json

from fastapi.testclient import TestClient

from web.app import app


def test_prefs_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("GLYPH_DATA_ROOT", str(tmp_path))
    client = TestClient(app)
    assert client.get("/api/prefs").json() == {}
    r = client.put("/api/prefs", json={"key": "appearance", "value": {"mode": "dark"}})
    assert r.status_code == 200
    assert client.get("/api/prefs").json()["appearance"]["mode"] == "dark"
    # 落盘可读
    saved = json.loads((tmp_path / "prefs.json").read_text("utf-8"))
    assert saved["appearance"]["mode"] == "dark"


def test_prefs_rejects_unknown_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GLYPH_DATA_ROOT", str(tmp_path))
    client = TestClient(app)
    r = client.put("/api/prefs", json={"key": "password", "value": "x"})
    assert r.status_code == 400
