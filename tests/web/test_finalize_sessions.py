"""定稿 / 会话 / ask-user 接口测试（从 test_server.py 拆出）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import web.server as srv
from glyph.session import new_session
from glyph.session.records import FinalizePayload
from glyph.session.store import SessionStore
from glyph.tools.ask_user import AskUserGateway, AskUserTool
from web import runtime as web_runtime
from web.deps import state as web_state

# ---------- 定稿端点：直调 apply_decisions + finalize_text（MSG-010 ④） ----------


def _det(
    det_id: str,
    start,
    end,
    original: str,
    corrected: str,
    *,
    aligned: bool = True,
    recheck_status: str | None = None,
) -> dict:
    d = {
        "id": det_id,
        "start": start,
        "end": end,
        "original": original,
        "corrected": corrected,
        "type": "hard",
        "tier": "error",
        "explanation_zh": "讲解",
        "source": "model",
        "aligned": aligned,
    }
    if recheck_status:
        d["recheck"] = {"status": recheck_status, "round": 1, "note_zh": "复核说明"}
    return d


async def test_finalize_edited_override_and_withdrawn(web_env):
    text = "He go to school. It is very good."
    dets = [
        _det("d1", 0, 5, "He go", "He goes"),
        _det("d2", 21, 30, "very good", "great"),
        _det(
            "d3", None, None, "old phrase", "new phrase", aligned=False, recheck_status="withdrawn"
        ),
    ]
    req = srv.FinalizeRequest(
        text=text,
        detections=dets,
        decisions={"d1": "edited", "d2": "rejected", "d3": "accepted"},
        overrides={"d1": "He went"},
    )
    resp = await srv.finalize(req)
    assert resp["final_text"] == "He went to school. It is very good."
    assert [d["id"] for d in resp["kept"]] == ["d1"]
    assert any("已撤销" in w for w in resp["warnings"])


async def test_finalize_unaligned_kept_but_not_applied(web_env):
    text = "It is very good."
    dets = [_det("d1", None, None, "very good", "great", aligned=False)]
    req = srv.FinalizeRequest(
        text=text,
        detections=dets,
        decisions={"d1": "edited"},
        overrides={"d1": "wonderful"},
    )
    resp = await srv.finalize(req)
    assert resp["final_text"] == text  # 定位不到，不动原文
    assert [d["id"] for d in resp["kept"]] == ["d1"]
    assert any("未自动套用" in w for w in resp["warnings"])


async def test_finalize_untouched_ids_are_dropped(web_env):
    """没表态的条目保守剔除（review.py 语义），端点透传不额外发明规则。"""
    text = "He go to school."
    dets = [_det("d1", 0, 5, "He go", "He goes")]
    req = srv.FinalizeRequest(text=text, detections=dets, decisions={}, overrides=None)
    resp = await srv.finalize(req)
    assert resp["final_text"] == text
    assert resp["kept"] == []


# ---------- 定稿落流水（v2.1 #69 / A1，方案 §8.6） ----------


async def test_finalize_appends_to_active_session(web_env, monkeypatch):
    """活跃会话：带 grade_call_id 的定稿经内存 history 落账，回传 appended。"""
    captured: dict = {}

    class FakeHistory:
        session_id = "test-sid"

        def close(self) -> None:
            pass

        async def record_finalize(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(web_state, "history", FakeHistory())
    text = "He go to school."
    req = srv.FinalizeRequest(
        text=text,
        detections=[_det("d1", 0, 5, "He go", "He goes")],
        decisions={"d1": "accepted"},
        grade_call_id="call_1",
        session_id="test-sid",
    )

    resp = await srv.finalize(req)

    assert resp["finalize_record"] == "appended"
    assert captured["grade_call_id"] == "call_1"
    assert captured["kept_ids"] == ["d1"]
    assert captured["final_text"] == resp["final_text"]
    assert captured["decisions"] == {"d1": "accepted"}


async def test_finalize_without_call_id_skips_recording(web_env):
    """旧前端不带 grade_call_id：照常算并返回，但不落流水（fixture 的 history 没有
    record_finalize，误调会直接 AttributeError，反证不会落账）。"""
    text = "He go to school."
    req = srv.FinalizeRequest(
        text=text,
        detections=[_det("d1", 0, 5, "He go", "He goes")],
        decisions={"d1": "accepted"},
    )

    resp = await srv.finalize(req)

    assert resp["finalize_record"] == "skipped_no_call_id"
    assert resp["final_text"]  # 定稿成果照常返回，不因落账缺失而丢


async def test_finalize_all_cancelled_skips_recording(web_env):
    """整单撤销、没有任何采纳成果（kept 为空）：不落定稿记录。"""
    text = "He go to school."
    req = srv.FinalizeRequest(
        text=text,
        detections=[_det("d1", 0, 5, "He go", "He goes")],
        decisions={"d1": "cancelled"},
        grade_call_id="call_9",
    )

    resp = await srv.finalize(req)

    assert resp["kept"] == []
    assert resp["finalize_record"] == "skipped_nothing_kept"


async def test_finalize_persists_to_inactive_session_store(web_env, tmp_path, monkeypatch):
    """定稿落在一个当前没打开的旧会话：不起 runtime，用独立 SessionStore 只追加，
    seq 接文件末尾；重开能按 call_id 读到最新定稿。"""
    history = await new_session(system_prompt="s", model="m", cwd=".", root=tmp_path)
    sid = history.session_id
    path = history.path
    history.close()
    monkeypatch.setattr(web_runtime, "session_path", lambda _sid: path)

    text = "He go to school."
    req = srv.FinalizeRequest(
        text=text,
        detections=[_det("d1", 0, 5, "He go", "He goes")],
        decisions={"d1": "accepted"},
        grade_call_id="call_old",
        session_id=sid,
    )

    resp = await srv.finalize(req)
    assert resp["finalize_record"] == "appended"

    store = SessionStore(path)
    read = store.read_records()
    fin = [r for r in read.records if r.record_type and r.type == "finalize"]
    assert len(fin) == 1
    payload = fin[0].payload_as(FinalizePayload)
    assert payload.grade_call_id == "call_old"
    assert payload.kept_ids == ["d1"]
    assert fin[0].seq == max(r.seq for r in read.records)  # seq 接续末尾
    store.close()


# ---------- 会话 id 净化：不接受能跳出会话目录的编号（P0 安全） ----------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../evil",
        "..%2f..%2fevil",
        "session-",
        "",
        "session-20260927-010000-abcdef/../../../x",
    ],
)
async def test_session_id_must_match_pattern(web_env, bad_id):
    """形如 ../ 的编号不得被拼进路径：既不能读，也不能删。"""
    with pytest.raises(srv.HTTPException) as exc:
        await srv.get_session_messages(bad_id)
    assert exc.value.status_code == 404

    with pytest.raises(srv.HTTPException) as exc2:
        await srv.delete_session_endpoint(bad_id)
    assert exc2.value.status_code == 404


async def test_batch_delete_ignores_malformed_ids(web_env):
    """批量删除里混进非法编号：直接忽略，不去碰磁盘。"""
    req = srv.BatchDeleteRequest(session_ids=["../../evil", "session-"])
    resp = await srv.batch_delete_sessions(req)
    assert resp["deleted"] == []


# ---------- /api/sessions：列表只列「开过口」的会话 ----------

# 契约来源：2026-10-12 人反馈「还没说话，列表里就多了个未命名会话」。
# 空会话的唯一来源是各处自动兜底（启动自建 / 删除当前后重开），界面上纯属噪音；
# 侧栏空态文案写的就是「问过一次就会出现在这里」。判据是"有没有 user_message
# 记录"，**不是** title 是否为空——只发图没打字的会话也开过口，不能错杀。


def _mk_summary(tmp_path: Path, sid: str, lines: list[str]):
    p = tmp_path / f"{sid}.jsonl"
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return SimpleNamespace(
        session_id=sid, path=p, created_at="2026-10-12T00:00:00", model="m"
    )


async def _list_with(monkeypatch, tmp_path, summaries):
    monkeypatch.setattr(web_runtime, "iter_session_summaries", lambda *a, **kw: summaries)
    web_state.title_cache.clear()
    return await srv.list_sessions()


async def test_list_sessions_hides_never_spoken_session(web_env, tmp_path, monkeypatch):
    spoken = _mk_summary(
        tmp_path,
        "sid-spoken",
        [json.dumps({"type": "user_message", "payload": {"text": "帮我批改"}}, ensure_ascii=False)],
    )
    empty = _mk_summary(tmp_path, "sid-empty", [])
    resp = await _list_with(monkeypatch, tmp_path, [spoken, empty])
    assert [s["session_id"] for s in resp["sessions"]] == ["sid-spoken"]
    assert resp["sessions"][0]["title"] == "帮我批改"


async def test_list_sessions_keeps_image_only_session(web_env, tmp_path, monkeypatch):
    """只发图没打字：有 user_message（text 为空）= 开过口，必须列。"""
    spoken = _mk_summary(
        tmp_path, "sid-img", [json.dumps({"type": "user_message", "payload": {"text": ""}})]
    )
    resp = await _list_with(monkeypatch, tmp_path, [spoken])
    assert [s["session_id"] for s in resp["sessions"]] == ["sid-img"]
    assert resp["sessions"][0]["title"] == ""


async def test_list_sessions_still_lists_unreadable_file(web_env, tmp_path, monkeypatch):
    """文件打不开（OSError）≠ 没说过话：照旧列出（未命名），不许静默藏掉。"""
    summary = SimpleNamespace(
        session_id="sid-gone",
        path=tmp_path / "nope.jsonl",
        created_at="2026-10-12T00:00:00",
        model="m",
    )
    resp = await _list_with(monkeypatch, tmp_path, [summary])
    assert [s["session_id"] for s in resp["sessions"]] == ["sid-gone"]


async def test_session_id_cannot_escape_sessions_root(web_env, tmp_path, monkeypatch):
    """真实越界：会话根之外躺着一个 .jsonl，用 ../ 编号去删它 —— 必须删不掉。

    session_path 会把 id 直接拼进路径，不校验就等价于"能删任意位置的 .jsonl"。
    """
    root = tmp_path / "sessions"
    monkeypatch.setenv("GLYPH_SESSIONS_ROOT", str(root))
    victim = tmp_path / "evil.jsonl"  # 会话根之外
    victim.write_text("{}\n", encoding="utf-8")

    with pytest.raises(srv.HTTPException):
        await srv.delete_session_endpoint("../../../../evil")

    assert victim.exists(), "越界文件被删掉了 —— 路径穿越成立"


# ---------- 定稿：脏数据要给 422 人话，不能裸 500 ----------


async def test_finalize_malformed_detection_is_422(web_env):
    """detections 字段类型/取值非法：给人话 422，而不是没有 detail 的 500。

    否则老师点"定稿"直接白屏，前面逐条点的表态全丢。
    """
    req = srv.FinalizeRequest(
        text="He go to school.",
        detections=[{"id": "d1", "start": "不是数字", "type": "不存在的类型"}],
        decisions={},
    )
    with pytest.raises(srv.HTTPException) as exc:
        await srv.finalize(req)
    assert exc.value.status_code == 422
    assert exc.value.detail  # 有内容，能让前端直接显示


# 会话 id 必须是本项目自己生成的形状（session-<日期>-<时间>-<短id>），
# 否则服务端在入口就 404，测不到"翻本子"这件事本身。
_SID_A = "session-20261010-230000-aaaaaaaa"
_SID_B = "session-20261010-230000-bbbbbbbb"
_SID_GONE = "session-20261010-230000-dddddddd"
_SID_FRESH = "session-20261010-230000-cccccccc"


class _FakeLoop:
    """够用的 loop 替身：只记录"谁跑了什么 / 有没有被掐"，不碰真模型。"""

    def __init__(self) -> None:
        self.busy = False
        self.messages: list[str] = []
        self.cancelled = False

    def run(self, message: str):
        self.messages.append(message)

        async def _gen():  # 空事件流：只为让 run 真正被调用一次
            if False:
                yield None

        return _gen()

    def cancel(self) -> None:
        self.cancelled = True


def _frames(body: str) -> list[dict]:
    """把 SSE 正文还原成事件字典列表（心跳注释帧自然跳过）。"""
    out: list[dict] = []
    for part in body.split("\n\n"):
        line = part.strip()
        if not line.startswith("data:"):
            continue
        out.append(json.loads(line[5:].strip()))
    return out


async def test_ensure_current_session_switches_to_requested_id(web_env, monkeypatch):
    """前端带 session_id 来：服务端必须先把"当前本子"翻到那个会话，再跑。

    前端每个标签页各持一个 id，而服务端只有一个当前槽位。不对齐就会出现
    「A 说的话写进 B 的流水」——写错本子不报错，回头根本查不到。
    """
    opened: list[str] = []

    async def _fake_open(sid, **_kw):
        opened.append(sid)
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *a, **kw: _FakeLoop())

    actual = await web_runtime._ensure_current_session(_SID_B)

    assert opened == [_SID_B]
    assert actual == _SID_B
    assert web_state.session_id == _SID_B


async def test_ensure_current_session_falls_back_to_fresh_when_id_gone(web_env, monkeypatch):
    """id 打不开（服务端重启过、前端还攥着旧 id）：新建会话并把新 id 如实回传。

    回传很关键——前端不知道 id 换了就会一直拿旧 id 来，每次都被当成"打不开"
    而新建一个，会话数会静默爆炸。
    """

    async def _boom(sid, **_kw):
        raise srv.SessionNotFoundError(sid)

    async def _fake_new(**_kw):
        return SimpleNamespace(session_id=_SID_FRESH, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _boom)
    monkeypatch.setattr(web_runtime, "new_session", _fake_new)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *a, **kw: _FakeLoop())

    actual = await web_runtime._ensure_current_session(_SID_GONE)

    assert actual == _SID_FRESH
    assert web_state.session_id == _SID_FRESH


async def test_ask_binds_requested_session_and_says_so(web_env, monkeypatch):
    """/api/ask 收到的 session_id 决定这一轮写进哪；并把实际会话 id 发回前端。"""
    loop_b = _FakeLoop()

    async def _fake_open(sid, **_kw):
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *a, **kw: loop_b)

    resp = await srv.ask(text="你好", images=None, study_context=None, session_id=_SID_B)
    body = "".join([chunk async for chunk in resp.body_iterator])

    events = _frames(body)
    assert events[0] == {"type": "session_bound", "session_id": _SID_B}
    assert loop_b.messages == ["你好"]
    assert web_state.session_id == _SID_B


async def test_ask_emits_queued_frame_while_session_gate_is_held(web_env, monkeypatch):
    """闸被上一轮占着时，新请求必须在等待期间先把"已排队"吐给前端，不能一声不吭（BUG-03）。

    上次现象：断连后旧请求最长占闸约 15s（要等下一次心跳写失败才被回收），早上来的新请求
    在 `async with _session_gate` 处干等，连第一帧都不发，用户以为卡死。修法：取闸带超时，
    超时就先吐一帧排队提示，等待期间始终有可见反馈。
    """
    loop_b = _FakeLoop()

    async def _fake_open(sid, **_kw):
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *a, **kw: loop_b)
    # 免得测试干等真实阈值；只验"超时会吐帧"这一契约
    monkeypatch.setattr(web_runtime, "_QUEUE_NOTICE_SECONDS", 0.05)

    # 闸是**每会话一把**（并发改造后不再是全局那把）：先把会话放进池，再占住它自己的闸
    session = web_runtime._pool_put(
        web_runtime._make_session(SimpleNamespace(session_id=_SID_B, close=lambda: None))
    )
    await session.gate.acquire()  # 模拟"这个会话的上一轮还没收尾"
    resp = await srv.ask(text="你好", images=None, study_context=None, session_id=_SID_B)
    it = resp.body_iterator.__aiter__()
    try:
        first = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        assert '"queued"' in first  # 等待期间先给反馈，而不是静默阻塞
    finally:
        session.gate.release()
        await it.aclose()


async def test_ask_does_not_emit_queued_frame_when_gate_is_free(web_env, monkeypatch):
    """闸空闲时（正常路径）不许凭空多出一帧排队提示：第一帧仍是 session_bound。"""
    loop_b = _FakeLoop()

    async def _fake_open(sid, **_kw):
        return SimpleNamespace(session_id=sid, close=lambda: None)

    monkeypatch.setattr(web_runtime, "open_session", _fake_open)
    monkeypatch.setattr(web_runtime, "_make_loop", lambda *a, **kw: loop_b)

    resp = await srv.ask(text="你好", images=None, study_context=None, session_id=_SID_B)
    body = "".join([chunk async for chunk in resp.body_iterator])
    assert _frames(body)[0] == {"type": "session_bound", "session_id": _SID_B}


async def test_cancel_other_session_leaves_current_run_alone(web_env, monkeypatch):
    """别人的停止按钮不能掐掉当前这个 run（/api/cancel 必须认会话）。"""
    loop = _FakeLoop()
    monkeypatch.setattr(web_state, "loop", loop)
    monkeypatch.setattr(web_state, "session_id", _SID_A)

    other = await srv.cancel(session_id=_SID_B)
    assert loop.cancelled is False
    assert other["ok"] is False

    mine = await srv.cancel(session_id=_SID_A)
    assert loop.cancelled is True
    assert mine["ok"] is True


# ---------- /api/ask-user/answer：把用户的答复送回挂起中的 ask_user 工具 ----------

# 契约来源：docs 未单列，设计见 .workbuddy/memory/2026-10-08.md「询问用户 ask_user」。
# 三条硬边界：绝不伪造答案 / 自由输入优先于选项 / 没有待回答的问题就别硬塞。


async def test_answer_endpoint_delivers_choice_to_pending_ask(web_env, monkeypatch):
    """用户点了某个选项：答案要真的落到那个挂起中的工具手上。"""
    gateway = AskUserGateway()
    monkeypatch.setattr(web_state, "ask_gateway", gateway)
    tool = AskUserTool(gateway)
    task = asyncio.ensure_future(
        tool.run(
            {
                "question": "这篇要先改还是先讲思路？",
                "options": [
                    {"label": "先批改", "description": "逐句细改"},
                    {"label": "先讲思路", "description": "只说整体方向"},
                ],
            }
        )
    )
    await asyncio.sleep(0)  # 让工具把待回答挂上
    assert gateway.has_pending is True

    resp = await srv.answer_ask_user(srv.AskUserAnswerRequest(choice="先批改"))

    assert resp["ok"] is True
    out = await asyncio.wait_for(task, timeout=1.0)
    assert out["status"] == "answered"
    assert out["choice"] == "先批改"
    assert "先批改" in out["summary"]


async def test_answer_endpoint_prefers_custom_text_over_choice(web_env, monkeypatch):
    """用户自己打了字：按"他自己说明的"处理，而不是当成选了某个选项。

    他很可能是在改需求（"先别改，讲思路"），当成选了"先批改"就完全反了。
    """
    gateway = AskUserGateway()
    monkeypatch.setattr(web_state, "ask_gateway", gateway)
    tool = AskUserTool(gateway)
    task = asyncio.ensure_future(
        tool.run(
            {
                "question": "这篇要先改还是先讲思路？",
                "options": [
                    {"label": "先批改", "description": "逐句细改"},
                    {"label": "先讲思路", "description": "只说整体方向"},
                ],
            }
        )
    )
    await asyncio.sleep(0)

    resp = await srv.answer_ask_user(
        srv.AskUserAnswerRequest(choice="先批改", text="先别改，讲思路")
    )

    assert resp["ok"] is True
    out = await asyncio.wait_for(task, timeout=1.0)
    assert out["status"] == "answered"
    assert out["custom_text"] == "先别改，讲思路"


async def test_answer_endpoint_without_pending_question_is_noop(web_env, monkeypatch):
    """没有待回答的问题（重复提交 / 手慢了几秒）：回 ok=False，不报错。

    用 4xx 会让前端弹出一个用户看不懂的错误；实际上这只是一次迟到的点击，
    什么都不做就是最对的做法。
    """
    monkeypatch.setattr(web_state, "ask_gateway", AskUserGateway())

    resp = await srv.answer_ask_user(srv.AskUserAnswerRequest(choice="先批改"))

    assert resp["ok"] is False


async def test_answer_endpoint_rejects_empty_answer(web_env, monkeypatch):
    """既不点选项也不打字：什么都不算，问题继续挂着等，绝不冒充"用户选了空"。 """
    gateway = AskUserGateway()
    monkeypatch.setattr(web_state, "ask_gateway", gateway)
    tool = AskUserTool(gateway)
    asyncio.ensure_future(
        tool.run(
            {
                "question": "这篇要先改还是先讲思路？",
                "options": [
                    {"label": "先批改", "description": "逐句细改"},
                    {"label": "先讲思路", "description": "只说整体方向"},
                ],
            }
        )
    )
    await asyncio.sleep(0)

    resp = await srv.answer_ask_user(srv.AskUserAnswerRequest())

    assert resp["ok"] is False
    assert gateway.has_pending is True  # 问题还在等，没被空答复顶掉


def test_build_runtime_wires_the_same_gateway_into_registry(monkeypatch, tmp_path):
    """服务层拿的通道必须和注册表里那个 ask_user 是**同一个**。

    这条守住一种最阴的失效：端点接好了、工具也挂起了，但两者不是同一条通道 ——
    用户点了选项，界面上什么反应都没有，而日志一路安静。
    """
    captured: dict = {}
    real = web_runtime.build_default_registry

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(web_runtime, "build_default_registry", spy)
    monkeypatch.setattr(web_state, "upload_dir", tmp_path)

    _, registry, *_ = web_runtime._build_runtime(srv.load_settings())

    assert captured.get("ask_gateway") is web_state.ask_gateway
    ask_tool = registry.get("ask_user")
    assert ask_tool is not None
    assert ask_tool.gateway is web_state.ask_gateway


