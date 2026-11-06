"""AttemptStore 落盘 / 幂等 / 软删 / 过滤 / 预算截断（T07，离线）。

与 MemoryStore 同一套保证：原子写、平铺 markdown、坏文件跳过计数、软删进
``.trash`` 绝不硬删。T07 额外守两条：

- **幂等**：attempt_id 由内容派生，同一条作答 save 两次只产生一个文件，
  重试 / 重复点击不会记两条，且首次确认时间不被覆盖；
- **读取有限**：按知识点精确过滤 + 最近时间倒序 + 条数上限 + 总字节预算，
  绝不把全部历史塞回上下文。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from glyph.attempts import AttemptNotFoundError, Outcome, build_attempt
from glyph.attempts.store import AttemptStore

_T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)


def _record(answer: str = "goed", *, skill: str = "一般过去时", **over: object):
    base = {
        "session_id": "session-20261003-120000-abcdef",
        "skill": skill,
        "prompt": "Yesterday I ___ (go) home.",
        "answer": answer,
        "hint_used": False,
        "outcome": "needs_practice",
        "evidence": "go 的过去式是 went。",
        "confirmation": "好的，记下来",
    }
    base.update(over)
    return build_attempt(now=_T0, **base)


def _store(root: Path, *, times: list[datetime] | None = None) -> AttemptStore:
    """构造 store；times 给定时，每次 save 的 confirmed_at 依次取其中时刻。"""
    if times is None:
        times = [_T0]
    clock = iter(list(times))
    return AttemptStore(root, clock=lambda: next(clock))


def test_save落盘且可读回_字段完整(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = _record()
    action, saved = store.save(rec)
    assert action == "created"
    assert saved.confirmed_at == _T0  # 落盘时补上确认时间

    again = AttemptStore(tmp_path).get(saved.attempt_id)
    assert again.attempt_id == saved.attempt_id
    assert again.skill == "一般过去时"
    assert again.prompt == "Yesterday I ___ (go) home."
    assert again.answer == "goed"
    assert again.hint_used is False
    assert again.outcome is Outcome.NEEDS_PRACTICE
    assert again.evidence == "go 的过去式是 went。"
    assert again.confirmation == "好的，记下来"
    assert again.session_id == "session-20261003-120000-abcdef"
    assert again.confirmed_at == _T0


def test_save幂等_同一条两次只一个文件且确认时间不覆盖(tmp_path: Path) -> None:
    t1, t2 = _T0, _T0 + timedelta(hours=1)
    store = _store(tmp_path, times=[t1, t2])
    rec = _record()
    first_action, first = store.save(rec)
    second_action, second = store.save(rec)  # 网络重试：同一份作答再交一次

    assert first_action == "created"
    assert second_action == "exists"
    assert first.attempt_id == second.attempt_id
    files = list(tmp_path.glob("attempt-*.md"))
    assert len(files) == 1
    # 首次确认时间保留，不被重试时刻覆盖（可追溯边界）
    assert AttemptStore(tmp_path).get(first.attempt_id).confirmed_at == t1


def test_不同作答是两条记录(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0, _T0])
    store.save(_record("goed"))
    store.save(_record("went"))
    assert len(list(tmp_path.glob("attempt-*.md"))) == 2


def test_get不存在抛NotFound(tmp_path: Path) -> None:
    with pytest.raises(AttemptNotFoundError):
        AttemptStore(tmp_path).get("0123456789ab")


def test_软删移入trash_活目录不再含且可追溯(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0, _T0])  # save 一次、软删时间戳再一次
    _, saved = store.save(_record())
    aid = saved.attempt_id

    moved = store.soft_delete(aid)
    assert moved.exists()
    assert aid in moved.name and moved.parent == tmp_path / ".trash"
    assert list(tmp_path.glob("attempt-*.md")) == []
    with pytest.raises(AttemptNotFoundError):
        store.get(aid)
    # 删一个已经不在的 id：报错，不静默成功
    with pytest.raises(AttemptNotFoundError):
        store.soft_delete(aid)


def test_list按确认时间倒序(tmp_path: Path) -> None:
    times = [_T0, _T0 + timedelta(hours=1), _T0 + timedelta(hours=2)]
    store = _store(tmp_path, times=times)
    store.save(_record("a"))
    store.save(_record("b"))
    store.save(_record("c"))

    page = store.list(limit=10, total_max_bytes=10**9)
    answers = [r.answer for r in page.records]
    assert answers == ["c", "b", "a"]  # 最新在前
    assert page.truncated is False


def test_list按知识点精确过滤且归一空白(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0, _T0, _T0])
    store.save(_record("a", skill="一般过去时"))
    store.save(_record("b", skill="一般过去时"))
    store.save(_record("c", skill="冠词"))

    page = store.list(skill=" 一般过去时\n", limit=10, total_max_bytes=10**9)
    assert len(page.records) == 2
    assert {r.answer for r in page.records} == {"a", "b"}


def test_list无该知识点返回空(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0])
    store.save(_record("a", skill="一般过去时"))
    page = store.list(skill="虚拟语气", limit=10, total_max_bytes=10**9)
    assert page.records == []


def test_list条数上限截断(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0 + timedelta(hours=i) for i in range(5)])
    for word in ["a", "b", "c", "d", "e"]:
        store.save(_record(word))
    page = store.list(limit=3, total_max_bytes=10**9)
    assert len(page.records) == 3
    assert page.truncated is True
    # 截的是更早的：留下最近三条 e d c
    assert [r.answer for r in page.records] == ["e", "d", "c"]


def test_list总字节预算截断(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0 + timedelta(hours=i) for i in range(3)])
    for word in ("a", "b", "c"):
        store.save(_record(word, evidence="go 的过去式是 went，" * 20))
    # 条数给够，但字节预算只够装下最新一条
    page = store.list(limit=10, total_max_bytes=200)
    assert page.truncated is True
    assert len(page.records) < 3
    assert page.records[0].answer == "c"  # 预算优先保最近的


def test_list坏文件跳过并计数_好记录照常(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0])
    store.save(_record("good"))
    # 手写一个损坏条目（缺 outcome、正文也不全）
    (tmp_path / "attempt-deadbeefdead.md").write_text(
        "---\nversion: 1\nattempt_id: deadbeefdead\n---\n\n## 题目\n坏的\n",
        encoding="utf-8",
    )
    page = store.list(limit=10, total_max_bytes=10**9)
    assert page.skipped_files == 1
    assert [r.answer for r in page.records] == ["good"]


def test_重启store同目录记录仍可读_持久(tmp_path: Path) -> None:
    _, saved = _store(tmp_path).save(_record())
    # 模拟进程重启 / 下次开会话：换一个全新 store 实例指同一目录
    reopened = AttemptStore(tmp_path)
    got = reopened.get(saved.attempt_id)
    assert got.answer == "goed"
    assert len(reopened.list(limit=10, total_max_bytes=10**9).records) == 1


def test_list知识点相关性匹配_泛主题命中带修饰的具体点(tmp_path: Path) -> None:
    """真机 BUG-3：record 时模型常把知识点写具体（带括号 / 英文修饰），read 时用
    泛主题，精确相等会漏召回（真机上「一般过去时」读不到「一般过去时（不规则动词
    go → went）」）。泛查询应命中以它为主题的更具体记录，反向也成立。"""
    store = _store(tmp_path, times=[_T0, _T0, _T0])
    store.save(_record("a", skill="一般过去时（不规则动词 go → went）"))
    store.save(_record("b", skill="一般过去时"))
    store.save(_record("c", skill="现在完成时"))

    # 泛主题查询命中两条"一般过去时"相关记录（含带括号修饰的那条），不碰无关的 c
    page = store.list(skill="一般过去时", limit=10, total_max_bytes=10**9)
    assert {r.answer for r in page.records} == {"a", "b"}
    # 反向：用更具体的查询，也能命中只写了泛主题的记录 b
    page2 = store.list(skill="一般过去时不规则动词", limit=10, total_max_bytes=10**9)
    assert {r.answer for r in page2.records} == {"b"}


def test_list知识点过短不做子串_避免片段误伤(tmp_path: Path) -> None:
    store = _store(tmp_path, times=[_T0])
    store.save(_record("a", skill="一般过去时"))
    # 不足 3 个字符的片段不做子串匹配（否则"过去"这种泛片段会误命中一堆知识点）
    assert store.list(skill="过去", limit=10, total_max_bytes=10**9).records == []
    # 3 个字符的稳定片段可以按相关性命中
    hit = store.list(skill="过去时", limit=10, total_max_bytes=10**9).records
    assert [r.answer for r in hit] == ["a"]
