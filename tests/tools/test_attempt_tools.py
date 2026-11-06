"""T07 三个尝试记录工具（record / read / delete）行为测试（离线）。

走真实 ``ToolRegistry.execute``，把 JSON Schema 闸门、R10 只读 / 写入身份、
确认闸门、幂等、跨会话读取一起测掉。存储用真 ``AttemptStore`` 落临时目录。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from glyph.attempts import AttemptStore
from glyph.runtime.registry import ToolRegistry
from glyph.tools.attempt_tools import (
    DeleteAttemptTool,
    ReadAttemptsTool,
    RecordAttemptTool,
)

_T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
_SID = "session-20261003-120000-abcdef"


def _args(**over: object) -> dict:
    base = {
        "skill": "一般过去时",
        "prompt": "Yesterday I ___ (go) home.",
        "answer": "goed",
        "hint_used": False,
        "outcome": "needs_practice",
        "evidence": "go 的过去式是 went，用户写成 goed。",
        "confirmation": "好的，记下来",
    }
    base.update(over)
    return base


def _registry(
    tmp_path: Path,
    *,
    sid: str | None = _SID,
    times: list[datetime] | None = None,
    read_limit: int = 5,
    total_max_bytes: int = 10**9,
) -> tuple[ToolRegistry, AttemptStore]:
    clock = iter(list(times) if times else [_T0])
    store = AttemptStore(tmp_path, clock=lambda: next(clock))

    def provider() -> str | None:
        return sid

    reg = ToolRegistry()
    reg.register(RecordAttemptTool(store, session_id_provider=provider))
    reg.register(ReadAttemptsTool(store, read_limit=read_limit, total_max_bytes=total_max_bytes))
    reg.register(DeleteAttemptTool(store))
    return reg, store


# --------------------------------------------------------------- record_attempt


async def test_record完整参数落盘并返回id(tmp_path: Path) -> None:
    reg, _ = _registry(tmp_path)
    res = await reg.execute("record_attempt", _args())
    assert res.ok, res.error
    out = res.output
    assert out["action"] == "created"
    assert len(out["attempt_id"]) == 12
    assert out["outcome"] == "needs_practice"


async def test_record没有活动会话被拒且不落盘(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path, sid=None)
    res = await reg.execute("record_attempt", _args())
    assert res.ok is False
    assert "会话" in res.error
    assert list(store.root.glob("attempt-*.md")) == []


async def test_record重复提交幂等只一条(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path, times=[_T0, _T0])
    first = await reg.execute("record_attempt", _args())
    second = await reg.execute("record_attempt", _args())
    assert first.ok and second.ok
    assert first.output["action"] == "created"
    assert second.output["action"] == "exists"
    assert first.output["attempt_id"] == second.output["attempt_id"]
    assert len(list(store.root.glob("attempt-*.md"))) == 1


async def test_record缺confirmation被schema闸门拒绝(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path)
    args = _args()
    del args["confirmation"]
    res = await reg.execute("record_attempt", args)
    assert res.ok is False
    assert "confirmation" in res.error
    assert list(store.root.glob("attempt-*.md")) == []


async def test_record纯空白确认不算确认(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path)
    res = await reg.execute("record_attempt", _args(confirmation="   "))
    assert res.ok is False
    assert list(store.root.glob("attempt-*.md")) == []


async def test_record坏outcome被枚举拒绝(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path)
    res = await reg.execute("record_attempt", _args(outcome="mastered"))
    assert res.ok is False
    assert list(store.root.glob("attempt-*.md")) == []


async def test_record字符串hint_used被类型拒绝(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path)
    res = await reg.execute("record_attempt", _args(hint_used="false"))
    assert res.ok is False
    assert list(store.root.glob("attempt-*.md")) == []


# ----------------------------------------------------------------- read_attempts


async def test_read无记录返回零条且明确不编造(tmp_path: Path) -> None:
    reg, _ = _registry(tmp_path)
    res = await reg.execute("read_attempts", {})
    assert res.ok
    assert res.output["count"] == 0
    assert res.output["attempts"] == []
    assert res.output["note"]  # 必须给模型一句"没有记录、别编上次"的话


async def test_read按知识点返回且跨会话_不回传确认原话会话id和正确答案(tmp_path: Path) -> None:
    # 记录发生在会话 A
    reg, _ = _registry(tmp_path, sid="session-A")
    await reg.execute("record_attempt", _args())

    # 读取工具不绑定当前会话：换个"新会话"的注册表（read 不拿 provider）也能读到
    store_b = AttemptStore(tmp_path)
    reg_b = ToolRegistry()
    reg_b.register(ReadAttemptsTool(store_b, read_limit=5, total_max_bytes=10**9))
    res = await reg_b.execute("read_attempts", {"skill": "一般过去时"})
    assert res.ok, res.error
    assert res.output["count"] == 1
    item = res.output["attempts"][0]
    assert item["answer"] == "goed"
    assert item["outcome"] == "needs_practice"
    assert item["prompt"].startswith("Yesterday")
    # 省 token + 隐私：回灌不给确认原话、不给内部会话 id
    assert "confirmation" not in item
    assert "session_id" not in item
    # 真机 BUG-4：evidence 里是正确答案（go→went），回灌会让模型在开场白照念、
    # 提前剧透新题要练的旧答案；出题靠题面 + 用户原错答 + 结果就够，不回传 evidence。
    assert "evidence" not in item
    assert {
        "attempt_id",
        "skill",
        "outcome",
        "hint_used",
        "confirmed_at",
        "prompt",
        "answer",
    } <= set(item)


async def test_read_limit被夹紧_模型要再多也不超上限(tmp_path: Path) -> None:
    times = [_T0 + timedelta(hours=i) for i in range(3)]
    reg, _ = _registry(tmp_path, times=times, read_limit=2)
    for word in ("a", "b", "c"):
        r = await reg.execute("record_attempt", _args(answer=word, evidence="x"))
        assert r.ok
    res = await reg.execute("read_attempts", {"limit": 999})
    assert res.ok
    assert len(res.output["attempts"]) == 2
    assert res.output["truncated"] is True


async def test_read不匹配知识点返回空(tmp_path: Path) -> None:
    reg, _ = _registry(tmp_path)
    await reg.execute("record_attempt", _args())
    res = await reg.execute("read_attempts", {"skill": "虚拟语气"})
    assert res.output["count"] == 0


# --------------------------------------------------------------- delete_attempt


async def test_delete缺确认被拒_记录仍在(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path)
    rec = await reg.execute("record_attempt", _args())
    aid = rec.output["attempt_id"]
    res = await reg.execute("delete_attempt", {"attempt_id": aid})  # 缺 confirmation
    assert res.ok is False
    assert len(list(store.root.glob("attempt-*.md"))) == 1


async def test_delete用户明确要求才软删进回收站(tmp_path: Path) -> None:
    reg, store = _registry(tmp_path, times=[_T0, _T0])
    aid = (await reg.execute("record_attempt", _args())).output["attempt_id"]
    res = await reg.execute("delete_attempt", {"attempt_id": aid, "confirmation": "这条删掉吧"})
    assert res.ok, res.error
    assert res.output["deleted"] is True
    assert list(store.root.glob("attempt-*.md")) == []
    assert len(list((store.root / ".trash").glob(f"attempt-{aid}-*.md"))) == 1
    # 删完读不到
    read = await reg.execute("read_attempts", {})
    assert read.output["count"] == 0


async def test_delete不存在的id报错不静默成功(tmp_path: Path) -> None:
    reg, _ = _registry(tmp_path)
    res = await reg.execute("delete_attempt", {"attempt_id": "0123456789ab", "confirmation": "删"})
    assert res.ok is False


# -------------------------------------------------------------------- R10 身份


def test_R10读写身份正确(tmp_path: Path) -> None:
    reg, _ = _registry(tmp_path)
    assert reg.is_readonly("read_attempts") is True
    assert reg.is_readonly("record_attempt") is False
    assert reg.is_readonly("delete_attempt") is False
