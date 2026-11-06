"""会话池测试（并发改造）：一会话一份运行时、每会话一把闸、超限淘汰。

改造前是「一个当前会话槽位 + 一把全局闸」：两个标签页只能排队，永远不会并行。
改造后要守住四条契约，任何一条破了都会退回串台或死锁：

1. 不同会话能**同时**跑（不再是"同一时刻只跑一个"）；
2. 同一个会话的两个请求仍然**串行**（同一个本子不能被两条流同时写）；
3. 池子上限到了淘汰最久没用的，但**绝不淘汰正在跑的**；
4. 「这一轮属于谁」走上下文变量：别的会话被激活也不会把当前这轮的会话改掉。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

import web.server as srv
from web import runtime as web_runtime
from web.deps import state as web_state

# 会话 id 必须先过入口的形状校验（session-<日期>-<时间>-<短id>），否则 404
_SID_A = "session-20260101-000001-aaaaaa"
_SID_B = "session-20260101-000002-bbbbbb"
_SID_SAME = "session-20260101-000003-cccccc"


def _drain(resp):
    """把这个 SSE 响应的流跑完（测试只需要它跑起来，不关心帧内容）。"""

    async def _run():
        async for _ in resp.body_iterator:
            pass

    return _run()


class _HoldLoop:
    """会卡住的假循环：用来观察「同一时刻到底有几个会话在跑」。"""

    def __init__(self) -> None:
        self.busy = False
        self.messages: list[str] = []
        self.active = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def run(self, message: str):
        self.messages.append(message)

        async def _gen():
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.entered.set()
            await self.release.wait()
            self.active -= 1
            return
            yield  # pragma: no cover - 只为让它成为异步生成器

        return _gen()

    def cancel(self) -> None:
        self.release.set()


def _fake_session_factory(sid: str, loops: dict[str, _HoldLoop]):
    """给 _ensure_session 的 open_session 替身：按会话 id 造流水，循环按 id 复用。"""

    async def _fake_open(opened_sid, **_kw):
        return SimpleNamespace(session_id=opened_sid, close=lambda: None)

    def _make_loop(*_a, **_kw):
        return loops[sid]

    return _fake_open, _make_loop


async def test_two_sessions_run_in_parallel(web_env, monkeypatch):
    """两个会话各跑各的：两个循环能同时进入 run（改造前这里只能是 1）。"""
    loops = {_SID_A: _HoldLoop(), _SID_B: _HoldLoop()}
    opened: list[str] = []

    async def _fake_open(sid, **_kw):
        opened.append(sid)
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(
        web_runtime, "_make_loop", lambda _s, _llm, _reg, history, **_kw: loops[history.session_id]
    )

    resp_a = await srv.ask(text="A 的话", images=None, study_context=None, session_id=_SID_A)
    resp_b = await srv.ask(text="B 的话", images=None, study_context=None, session_id=_SID_B)

    async def _drain(resp):
        async for _ in resp.body_iterator:
            pass

    task_a = asyncio.create_task(_drain(resp_a))
    task_b = asyncio.create_task(_drain(resp_b))
    await asyncio.wait_for(loops[_SID_A].entered.wait(), timeout=2.0)
    await asyncio.wait_for(loops[_SID_B].entered.wait(), timeout=2.0)

    # 两边都已经进到 run 里了 —— 这就是"并行"的直接证据
    assert loops[_SID_A].active == 1 and loops[_SID_B].active == 1
    assert loops[_SID_A].messages == ["A 的话"]
    assert loops[_SID_B].messages == ["B 的话"]

    loops[_SID_A].release.set()
    loops[_SID_B].release.set()
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)


async def test_same_session_still_serializes(web_env, monkeypatch):
    """同一个会话的两个请求必须排队：否则同一个流水被两条流同时写。"""
    loop = _HoldLoop()

    async def _fake_open(sid, **_kw):
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: loop)
    monkeypatch.setattr(web_runtime, "_QUEUE_NOTICE_SECONDS", 0.01)

    resp_1 = await srv.ask(text="第一句", images=None, study_context=None, session_id=_SID_SAME)
    resp_2 = await srv.ask(text="第二句", images=None, study_context=None, session_id=_SID_SAME)

    async def _drain(resp):
        async for _ in resp.body_iterator:
            pass

    task_1 = asyncio.create_task(_drain(resp_1))
    await asyncio.wait_for(loop.entered.wait(), timeout=2.0)
    task_2 = asyncio.create_task(_drain(resp_2))
    # 第二个请求还在等闸：此刻不该有第二条进 run
    await asyncio.sleep(0.05)
    assert loop.messages == ["第一句"]

    loop.release.set()
    await asyncio.wait_for(asyncio.gather(task_1, task_2), timeout=2.0)
    assert loop.messages == ["第一句", "第二句"]
    assert loop.peak == 1  # 全程都是串行的


async def test_pool_evicts_least_recently_used_only(web_env, monkeypatch):
    """池满先淘汰最久没用且不在跑的那个；正在跑的绝不淘汰。"""

    def _mk(sid: str) -> web_runtime.SessionRuntime:
        return web_runtime._make_session(SimpleNamespace(session_id=sid, close=lambda: None))

    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: SimpleNamespace(busy=False))
    monkeypatch.setattr(web_state, "settings", replace(web_state.settings, session_pool_max=2))

    first = _mk("one")
    second = _mk("two")
    web_runtime._pool_put(first)
    web_runtime._pool_put(second)
    assert list(web_state.sessions) == ["one", "two"]

    # 再用一下 one：它变成"最近用过"，淘汰时该轮到 two
    web_runtime._pool_get("one")
    web_runtime._pool_put(_mk("three"))
    assert list(web_state.sessions) == ["one", "three"]

    # 正在跑的那个不许淘汰：把 one 标成 busy，池子只能短暂超限
    web_state.sessions["one"].loop = SimpleNamespace(busy=True)
    web_runtime._pool_put(_mk("four"))
    assert "one" in web_state.sessions
    assert "three" not in web_state.sessions


async def test_activation_is_per_task_not_global(web_env, monkeypatch):
    """上下文变量绑定：别的会话被激活，也不能改掉「这一轮」的会话。"""
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: SimpleNamespace(busy=False))

    def _mk(sid: str) -> web_runtime.SessionRuntime:
        return web_runtime._make_session(SimpleNamespace(session_id=sid, close=lambda: None))

    session_a = _mk("a")
    session_b = _mk("b")

    # 两个任务各自绑定自己的会话，交替运行也不串
    async def _worker(session: web_runtime.SessionRuntime, seen: list[str]) -> None:
        token = web_runtime._activate(session)
        try:
            await asyncio.sleep(0)  # 让出一次，给另一个任务插入的机会
            seen.append(web_runtime.current_session_id() or "")
        finally:
            web_runtime._deactivate(token)

    seen_a: list[str] = []
    seen_b: list[str] = []
    await asyncio.gather(_worker(session_a, seen_a), _worker(session_b, seen_b))

    assert seen_a == ["a"]
    assert seen_b == ["b"]


async def test_study_context_is_per_session(web_env, monkeypatch):
    """学习状态摘要必须落在自己这一轮：别人更新进度不能覆盖我正在跑的那轮。"""
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: SimpleNamespace(busy=False))

    def _mk(sid: str) -> web_runtime.SessionRuntime:
        return web_runtime._make_session(SimpleNamespace(session_id=sid, close=lambda: None))

    session_a = _mk("a")
    session_a.study_context = {"book": "四级词书", "mode": "plan"}
    session_b = _mk("b")
    session_b.study_context = {"book": "六级词书", "mode": "plan"}

    token = web_runtime._activate(session_a)
    try:
        assert (web_runtime._current_study_context() or {}).get("book") == "四级词书"
        # 模拟另一个会话的请求把它的进度写进来
        web_runtime._sync_mirror(session_b)
        assert (web_runtime._current_study_context() or {}).get("book") == "四级词书"
    finally:
        web_runtime._deactivate(token)


@pytest.mark.parametrize("sid", ["bad/../id", "带空格 的"])
async def test_bad_session_id_rejected_before_stream(web_env, sid):
    """形状不对的会话 id 在入口就 404：不能进了 SSE 再抛，前端只看到流断了没终态。"""
    with pytest.raises(srv.HTTPException) as exc:
        await srv.ask(text="你好", images=None, study_context=None, session_id=sid)
    assert exc.value.status_code == 404


# ---------- 评审修出来的两个洞：不许重复开、不许删在跑的 ----------


async def test_open_existing_pooled_session_reuses_runtime(web_env, monkeypatch):
    """重复打开同一个会话：必须复用池里那份，绝不再开一份同文件流水。

    再开一份的后果：旧句柄泄漏（Windows 上之后删不掉文件）、同一会话两套流水两把闸
    （"同一会话串行"静默失效）、本轮学习进度被重置。
    """
    opened: list[str] = []

    async def _fake_open(sid, **_kw):
        opened.append(sid)
        return SimpleNamespace(
            session_id=sid,
            close=lambda: None,
            system_prompt_mismatch=False,
            skipped_lines=0,
        )

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: SimpleNamespace(busy=False))

    first = await srv.open_session_endpoint(srv.OpenSessionRequest(session_id=_SID_A))
    second = await srv.open_session_endpoint(srv.OpenSessionRequest(session_id=_SID_A))

    assert first == second == {"ok": True, "session_id": _SID_A}
    assert opened == [_SID_A]  # 只真开了一次
    assert len(web_state.sessions) == 1


async def test_delete_running_session_is_rejected(web_env, monkeypatch):
    """删一个正在跑的会话必须 409：不能把正在写的本子关掉再删文件。"""
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: SimpleNamespace(busy=False))
    session = web_runtime._pool_put(
        web_runtime._make_session(SimpleNamespace(session_id=_SID_A, close=lambda: None))
    )
    session.in_flight = 1  # 模拟"这一轮还在用"

    with pytest.raises(srv.HTTPException) as exc:
        await srv.delete_session_endpoint(_SID_A)
    assert exc.value.status_code == 409
    assert _SID_A in web_state.sessions  # 没被踢出池


async def test_settings_change_rejected_while_any_session_runs(web_env, monkeypatch):
    """任何会话在跑都不能换配置：否则「停下」按钮会落空（取消信号打到没人跑的新循环上）。"""
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: SimpleNamespace(busy=False))
    session = web_runtime._pool_put(
        web_runtime._make_session(SimpleNamespace(session_id=_SID_A, close=lambda: None))
    )
    session.loop = SimpleNamespace(busy=True)

    with pytest.raises(srv.HTTPException) as exc:
        await srv.set_config(srv.ConfigRequest())
    assert exc.value.status_code == 409


async def test_queued_request_does_not_steal_study_context(web_env, monkeypatch):
    """排队中的请求不许改写这一轮的进度摘要：上一轮还在用它渲染上下文。"""
    loop = _HoldLoop()

    async def _fake_open(sid, **_kw):
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *_a, **_kw: loop)
    monkeypatch.setattr(web_runtime, "_QUEUE_NOTICE_SECONDS", 0.01)

    first_ctx = '{"book": "四级词书", "mode": "plan"}'
    second_ctx = '{"book": "六级词书", "mode": "plan"}'
    resp_1 = await srv.ask(
        text="第一句", images=None, study_context=first_ctx, session_id=_SID_SAME
    )
    task_1 = asyncio.create_task(_drain(resp_1))
    await asyncio.wait_for(loop.entered.wait(), timeout=2.0)

    session = web_state.sessions[_SID_SAME]
    assert (session.study_context or {}).get("book") == "四级词书"

    resp_2 = await srv.ask(
        text="第二句", images=None, study_context=second_ctx, session_id=_SID_SAME
    )
    task_2 = asyncio.create_task(_drain(resp_2))
    await asyncio.sleep(0.05)  # 给排队的那个一点时间（它拿不到闸）
    assert (session.study_context or {}).get("book") == "四级词书"

    loop.release.set()
    await asyncio.wait_for(asyncio.gather(task_1, task_2), timeout=2.0)
