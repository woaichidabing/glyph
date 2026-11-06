"""read_artifact 只读取回工具单测（方案 §8.4，v2.1 #64）。

工具不直接持有会话，而是运行期经 provider 取"当前会话只读读取器"
（与 build_attempts 的 session_id_provider 同模式，registry 跨会话复用）。
安全：只读、无路径参数、不出当前会话；找不到给友好 ToolError 让模型换编号。
"""

from __future__ import annotations

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.artifact import ReadArtifactTool


class FakeReader:
    def __init__(self, *, catalog=None, artifacts=None, finals=None) -> None:
        self._catalog = catalog or {"count": 0, "items": []}
        self._artifacts = artifacts or {}
        self._finals = finals or {}

    def artifact_catalog(self):
        return self._catalog

    def read_artifact(self, call_id, *, item_id=None, which="result"):
        if which == "final":
            return self._finals.get(call_id)
        artifact = self._artifacts.get(call_id)
        if artifact is None:
            return None
        if item_id is not None:
            for d in artifact.get("detections", []):
                if d.get("id") == item_id:
                    return {"call_id": call_id, "item": d}
            return None
        return artifact


def _tool(reader):
    return ReadArtifactTool(history_provider=lambda: reader)


@pytest.mark.asyncio
async def test_no_call_id_returns_catalog():
    reader = FakeReader(catalog={"count": 1, "items": [{"call_id": "g1"}]})

    out = await _tool(reader).run({})

    assert out["count"] == 1
    assert out["items"][0]["call_id"] == "g1"


@pytest.mark.asyncio
async def test_read_full_result():
    reader = FakeReader(artifacts={"g1": {"call_id": "g1", "source_text": "He go."}})

    out = await _tool(reader).run({"call_id": "g1"})

    assert out["source_text"] == "He go."


@pytest.mark.asyncio
async def test_item_id_and_which_passed_through():
    reader = FakeReader(
        artifacts={"g1": {"detections": [{"id": "d1"}, {"id": "d2"}]}},
        finals={"g1": {"call_id": "g1", "final_text": "fixed"}},
    )
    tool = _tool(reader)

    assert (await tool.run({"call_id": "g1", "item_id": "d2"}))["item"]["id"] == "d2"
    assert (await tool.run({"call_id": "g1", "which": "final"}))["final_text"] == "fixed"


@pytest.mark.asyncio
async def test_unknown_call_id_is_friendly_error():
    with pytest.raises(ToolError) as exc:
        await _tool(FakeReader()).run({"call_id": "nope"})
    assert "nope" in str(exc.value)
    assert "目录" in str(exc.value)  # 提示先查目录


@pytest.mark.asyncio
async def test_missing_item_is_friendly_error():
    reader = FakeReader(artifacts={"g1": {"detections": [{"id": "d1"}]}})
    with pytest.raises(ToolError) as exc:
        await _tool(reader).run({"call_id": "g1", "item_id": "zz"})
    assert "zz" in str(exc.value)


@pytest.mark.asyncio
async def test_final_without_finalize_is_friendly_error():
    reader = FakeReader(artifacts={"g1": {"detections": []}})  # 有成品但无定稿
    with pytest.raises(ToolError) as exc:
        await _tool(reader).run({"call_id": "g1", "which": "final"})
    assert "定稿" in str(exc.value)


@pytest.mark.asyncio
async def test_no_active_session_is_friendly_error():
    tool = ReadArtifactTool(history_provider=lambda: None)
    with pytest.raises(ToolError):
        await tool.run({"call_id": "g1"})


@pytest.mark.asyncio
async def test_bad_which_rejected():
    reader = FakeReader(artifacts={"g1": {"detections": []}})
    with pytest.raises(ToolError):
        await _tool(reader).run({"call_id": "g1", "which": "etc_passwd"})


def test_declared_readonly():
    assert ReadArtifactTool(history_provider=lambda: None).readonly is True
