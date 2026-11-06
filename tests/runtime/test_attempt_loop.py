"""T07 端到端：确认写入 → 跨会话读取 → 出新题 → 再记录（离线、假模型）。

这是 T07 最在意的一条闭环，用真实 ``AgentLoop`` + 真实 ``AttemptStore``（落临时目录）
+ 剧本假模型走一遍，证明：

1. 会话 A 里模型调 record_attempt（带用户确认），工具真的落盘、结果回灌进下一轮；
2. **换一个全新会话**（新 loop、新空 history、新会话 id，但复用同一份磁盘存储），
   模型调 read_attempts 能读到会话 A 记下的尝试——跨会话持久成立；
3. 读到历史后出新题（不直接念旧答案），用户再作答，记第二条，两条内容 id 不同。

R10 身份与工具细节在 tests/tools/test_attempt_tools.py 守，这里只守闭环。
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeTurn, InMemoryHistory, ScriptedFakeStreamingLLM

from glyph.attempts import AttemptStore
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.tools.attempt_tools import (
    DeleteAttemptTool,
    ReadAttemptsTool,
    RecordAttemptTool,
)
from glyph.types import FinishReason, ToolMessage


def _register(store: AttemptStore, session_id: str) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RecordAttemptTool(store, session_id_provider=lambda: session_id)
    )
    registry.register(ReadAttemptsTool(store, read_limit=5, total_max_bytes=10**9))
    registry.register(DeleteAttemptTool(store))
    return registry


def _tool_messages(llm: ScriptedFakeStreamingLLM, call_index: int) -> list[ToolMessage]:
    messages = llm.calls[call_index][0]
    return [m for m in messages if isinstance(m, ToolMessage)]


async def _drain(loop: AgentLoop, user_text: str):
    events = [event async for event in loop.run(user_text)]
    return events


_RECORD_1 = {
    "skill": "一般过去时",
    "prompt": "Yesterday I ___ (go) home.",
    "answer": "goed",
    "hint_used": False,
    "outcome": "needs_practice",
    "evidence": "go 的过去式是 went，用户写成 goed。",
    "confirmation": "好的，记下来",
}

_RECORD_2 = {
    "skill": "一般过去时",
    "prompt": "Yesterday she ___ (go) to the market.",
    "answer": "went",
    "hint_used": False,
    "outcome": "independent_correct",
    "evidence": "went 正确，用户独立答对。",
    "confirmation": "对，这次记上",
}


async def test_确认写入_跨会话读取_再记录闭环(tmp_path: Path) -> None:
    store = AttemptStore(tmp_path)

    # ---------- 会话 A：短练习判完、用户确认，落第一条 ----------
    llm_a = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("record_attempt", _RECORD_1)]),
            FakeTurn(text="记好了，下次可以再练练一般过去时。"),
        ]
    )
    loop_a = AgentLoop(
        llm_a, _register(store, "session-A"), history=InMemoryHistory()
    )
    events_a = await _drain(loop_a, "我刚填了 goed，帮我记一下")

    assert events_a[-1].finish_reason == FinishReason.FINAL_TEXT
    page_after_a = store.list(limit=10, total_max_bytes=10**9)
    assert len(page_after_a.records) == 1
    first_id = page_after_a.records[0].attempt_id

    # 工具结果确实回灌给了模型（下一轮 stream_chat 的消息里有 record_attempt 结果）
    rec_msgs_a = _tool_messages(llm_a, 1)
    assert any(m.name == "record_attempt" and "created" in m.content for m in rec_msgs_a)

    # ---------- 会话 B：全新 loop / 空 history / 新会话 id，复用同一磁盘 ----------
    llm_b = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("read_attempts", {"skill": "一般过去时"})]),
            FakeTurn(text="我们接着练。Yesterday she ___ (go) to the market. 填什么？"),
            FakeTurn(tool_calls=[("record_attempt", _RECORD_2)]),
            FakeTurn(text="这次你独立答对了，went 用对了。"),
        ]
    )
    loop_b = AgentLoop(
        llm_b, _register(store, "session-B"), history=InMemoryHistory()
    )

    # 第一次用户回合：读历史 → 出新题（read 是只读，不应新增文件）
    events_b1 = await _drain(loop_b, "继续练一般过去时")
    assert events_b1[-1].finish_reason == FinishReason.FINAL_TEXT
    assert len(store.list(limit=10, total_max_bytes=10**9).records) == 1

    # 关键：出题前那一次模型调用，已经看到了会话 A 的旧作答（跨会话读取成立）
    rec_msgs_b = _tool_messages(llm_b, 1)
    read_results = [m for m in rec_msgs_b if m.name == "read_attempts"]
    assert read_results and "goed" in read_results[0].content
    assert "一般过去时" in read_results[0].content

    # 第二次用户回合：用户作答 → 记第二条
    events_b2 = await _drain(loop_b, "went")
    assert events_b2[-1].finish_reason == FinishReason.FINAL_TEXT

    final_page = store.list(limit=10, total_max_bytes=10**9)
    assert len(final_page.records) == 2
    ids = {r.attempt_id for r in final_page.records}
    assert len(ids) == 2  # 内容不同 → 两条身份不同
    assert first_id in ids
    assert {r.answer for r in final_page.records} == {"goed", "went"}
    outcomes = {r.outcome.value for r in final_page.records}
    assert outcomes == {"needs_practice", "independent_correct"}


async def test_同一条重复提交在循环里也只落一条(tmp_path: Path) -> None:
    """网络重试 / 用户连点：模型把同一条 record_attempt 交两次（不同轮），
    AttemptStore 幂等保证磁盘只有一条——主循环无需特殊处理。"""
    store = AttemptStore(tmp_path)
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("record_attempt", _RECORD_1)]),
            FakeTurn(text="好的。"),
            FakeTurn(tool_calls=[("record_attempt", _RECORD_1)]),  # 同参再交一次
            FakeTurn(text="已经记过了，不会重复。"),
        ]
    )
    loop = AgentLoop(llm, _register(store, "session-A"), history=InMemoryHistory())

    await _drain(loop, "记一下")
    await _drain(loop, "再帮我记一次同样的")

    page = store.list(limit=10, total_max_bytes=10**9)
    assert len(page.records) == 1
    # 第二次工具结果应是 exists（幂等命中），回灌给模型让它知道没重复存
    second_round = _tool_messages(llm, 3)
    assert any(m.name == "record_attempt" and "exists" in m.content for m in second_round)
