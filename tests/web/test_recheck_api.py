"""复核配额 / 复核端点测试（从 test_server.py 拆出）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.server as srv
from glyph.runtime.errors import ToolError
from glyph.tools.base import ReadOnlyTool
from web import runtime as web_runtime
from web.deps import state as web_state

# ---------- 复核配额：账本 + quota/reset 端点（§6.2 / §6.3，MSG-010 ③） ----------


async def test_quota_initial_shape(web_env):
    s = srv.load_settings()
    q = await srv.recheck_quota()
    assert q["per_item_max"] == s.recheck_max_rounds
    assert q["total_max"] == s.recheck_max_total
    assert q["total_used"] == 0
    assert q["total_remaining"] == s.recheck_max_total
    assert q["per_item_used"] == {}
    assert q["per_item_remaining"] == {}


async def test_quota_reflects_ledger(web_env):
    web_state.recheck_ledger["test-sid"] = {"per_item": {"d1": 1, "d2": 2}, "total": 3}
    s = srv.load_settings()
    q = await srv.recheck_quota()
    assert q["total_used"] == 3
    assert q["total_remaining"] == s.recheck_max_total - 3
    assert q["per_item_used"] == {"d1": 1, "d2": 2}
    assert q["per_item_remaining"]["d1"] == s.recheck_max_rounds - 1
    assert q["per_item_remaining"]["d2"] == 0


async def test_recheck_reset_clears_current_session(web_env):
    web_state.recheck_ledger["test-sid"] = {"per_item": {"d1": 1}, "total": 1}
    r = await srv.recheck_reset()
    assert r == {"ok": True}
    q = await srv.recheck_quota()
    assert q["total_used"] == 0
    assert q["per_item_used"] == {}


async def test_reset_does_not_touch_other_sessions(web_env):
    web_state.recheck_ledger["other-sid"] = {"per_item": {"d1": 1}, "total": 1}
    await srv.recheck_reset()
    assert web_state.recheck_ledger["other-sid"]["total"] == 1


# ---------- 复核配额闸：包装工具（单篇配额 web 独占负责，§6.4） ----------


class _FakeRecheckTool(ReadOnlyTool):
    name = "recheck_detection"
    description = "假复核工具（测试用）"
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def run(self, args: dict) -> dict:
        if self.fail:
            raise ToolError("复核模型暂时不可用")
        return {"id": args["detection"]["id"], "status": "upheld"}


def _wrap(inner, per=2, total=5):
    return web_runtime._QuotaGuardedRecheckTool(inner, max_per_item=per, max_total=total)


def _args(det_id: str, round_: int = 1) -> dict:
    return {"text": "He go to school.", "detection": {"id": det_id}, "round": round_}


async def test_wrapper_allows_and_counts(web_env):
    tool = _wrap(_FakeRecheckTool())
    await tool.run(_args("d1"))
    q = await srv.recheck_quota()
    assert q["per_item_used"] == {"d1": 1}
    assert q["total_used"] == 1


async def test_wrapper_blocks_per_item_exhausted(web_env):
    web_state.recheck_ledger["test-sid"] = {"per_item": {"d1": 2}, "total": 2}
    tool = _wrap(_FakeRecheckTool())
    with pytest.raises(ToolError):
        await tool.run(_args("d1"))


async def test_wrapper_blocks_total_exhausted(web_env):
    """单条还有余量、单篇用完：仍然拦（单篇配额 web 独占负责）。"""
    web_state.recheck_ledger["test-sid"] = {"per_item": {"d9": 5}, "total": 5}
    tool = _wrap(_FakeRecheckTool())
    with pytest.raises(ToolError):
        await tool.run(_args("d1"))


async def test_wrapper_does_not_count_inner_failure(web_env):
    """真工具自身失败（如断网）不递增配额——没复核成就不算次数。"""
    tool = _wrap(_FakeRecheckTool(fail=True))
    with pytest.raises(ToolError):
        await tool.run(_args("d1"))
    q = await srv.recheck_quota()
    assert q["total_used"] == 0
    assert q["per_item_used"] == {}


def test_wrapper_is_readonly_and_transparent():
    inner = _FakeRecheckTool()
    tool = _wrap(inner)
    assert tool.name == "recheck_detection"
    assert tool.description == inner.description
    assert tool.parameters == inner.parameters
    assert tool.readonly is True


# ---------- POST /api/recheck：批改台内直连复核（不经过对话 / 不经过模型转达） ----------


def _register_fake_recheck(fail: bool = False, per: int = 2, total: int = 5) -> _FakeRecheckTool:
    """把套了配额闸的假复核工具装进当前测试的注册表。"""
    inner = _FakeRecheckTool(fail=fail)
    web_state.registry.register(_wrap(inner, per=per, total=total))
    return inner


class _RecordingRecheck(_FakeRecheckTool):
    """记录每次调用收到的 args（用来钉 round/note 由端点正确下发）。"""

    def __init__(self):
        super().__init__(fail=False)
        self.calls: list[dict] = []

    async def run(self, args: dict) -> dict:
        self.calls.append(args)
        return {"id": args["detection"]["id"], "status": "upheld", "round": args["round"]}


async def test_recheck_endpoint_happy_path_rounds_and_quota(web_env):
    inner = _RecordingRecheck()
    web_state.registry.register(_wrap(inner))
    req = srv.RecheckApiRequest(
        text="He go to school.",
        detection={"id": "d1", "original": "go", "source": "model"},
        note="",
    )
    r1 = await srv.recheck_one(req)
    assert r1["ok"] is True
    assert r1["recheck"]["status"] == "upheld"
    assert r1["quota"]["total_used"] == 1

    # 第二次驳回同一条：round 由端点按账本自动算成 2，前端不需要传
    r2 = await srv.recheck_one(req)
    assert inner.calls[0]["round"] == 1
    assert inner.calls[1]["round"] == 2
    assert r2["quota"]["per_item_used"] == {"d1": 2}


async def test_recheck_endpoint_passes_optional_note(web_env):
    inner = _RecordingRecheck()
    web_state.registry.register(_wrap(inner))
    req = srv.RecheckApiRequest(
        text="He go to school.",
        detection={"id": "d1", "original": "go", "source": "model"},
        note="我觉得这里口语里可以这么说",
    )
    await srv.recheck_one(req)
    assert inner.calls[0]["note"] == "我觉得这里口语里可以这么说"


async def test_recheck_endpoint_quota_exhausted_is_409(web_env):
    _register_fake_recheck(per=2)
    web_state.recheck_ledger["test-sid"] = {"per_item": {"d1": 2}, "total": 2}
    req = srv.RecheckApiRequest(
        text="x", detection={"id": "d1", "original": "a", "source": "model"}
    )
    with pytest.raises(HTTPException) as ei:
        await srv.recheck_one(req)
    assert ei.value.status_code == 409


async def test_recheck_endpoint_tool_failure_is_502_and_not_counted(web_env):
    _register_fake_recheck(fail=True)
    req = srv.RecheckApiRequest(
        text="x", detection={"id": "d1", "original": "a", "source": "model"}
    )
    with pytest.raises(HTTPException) as ei:
        await srv.recheck_one(req)
    assert ei.value.status_code == 502
    # 没复核成不算次数（与配额闸口径一致）
    q = await srv.recheck_quota()
    assert q["total_used"] == 0


async def test_recheck_endpoint_without_registered_tool_is_503(web_env):
    # web_env 给的是空注册表（自定义纯文本端点等场景可能没有复核工具）
    req = srv.RecheckApiRequest(
        text="x", detection={"id": "d1", "original": "a", "source": "model"}
    )
    with pytest.raises(HTTPException) as ei:
        await srv.recheck_one(req)
    assert ei.value.status_code == 503


async def test_recheck_endpoint_empty_text_is_422_or_502(web_env):
    _register_fake_recheck()
    req = srv.RecheckApiRequest(
        text="   ", detection={"id": "d1", "original": "a", "source": "model"}
    )
    with pytest.raises(HTTPException) as ei:
        await srv.recheck_one(req)
    assert ei.value.status_code in (422, 502)


# ---------- 切会话自动重置账本（§6.3） ----------


async def test_switch_session_resets_ledger(monkeypatch, web_env):
    monkeypatch.setattr(
        srv, "_make_loop", lambda settings, llm, reg, h, **_kw: SimpleNamespace(busy=False)
    )
    web_state.recheck_ledger["s2"] = {"per_item": {"d1": 1}, "total": 1}
    fake_history = SimpleNamespace(session_id="s2", close=lambda: None)
    web_runtime._switch_session(fake_history)
    assert web_state.session_id == "s2"
    q = await srv.recheck_quota()
    assert q["total_used"] == 0
    assert q["per_item_used"] == {}


async def test_create_session_resets_ledger(monkeypatch, web_env):
    async def fake_new_session(**kw):
        return SimpleNamespace(session_id="s-new", close=lambda: None)

    monkeypatch.setattr(web_runtime, "new_session", fake_new_session)
    monkeypatch.setattr(
        srv, "_make_loop", lambda settings, llm, reg, h, **_kw: SimpleNamespace(busy=False)
    )
    # 预置同名残留：切过去必须归零
    web_state.recheck_ledger["s-new"] = {"per_item": {"d1": 2}, "total": 2}
    resp = await srv.create_session()
    assert resp["session_id"] == "s-new"
    q = await srv.recheck_quota()
    assert q["total_used"] == 0


