"""暂存区测试：提炼产物先落这里，等整理器合并（D29）。

暂存是**内部管道**：一个会话一个文件、只追加、用户看不见。
"""

from datetime import date

from glyph.memory.pending import (
    append_pending,
    list_pending,
    mark_done,
    pending_path,
)

_DAY = date(2026, 10, 15)


def test_append_creates_dated_file(tmp_path) -> None:
    path = append_pending(
        tmp_path,
        session_id="session-20261015-120000-abcd",
        title="帮我批改作文",
        lines=["> 帮我批改这篇", "< 好的", ". grade_text"],
        day=_DAY,
    )

    assert path.exists()
    assert path.parent.name == "15"
    assert path.parent.parent.name == "10"
    assert path.parent.parent.parent.name == "2026"
    body = path.read_text(encoding="utf-8")
    assert "> 帮我批改这篇" in body
    assert "< 好的" in body
    assert ". grade_text" in body


def test_append_is_pure_append(tmp_path) -> None:
    """同一天同一个会话再来一批，是追加不是覆盖。"""
    kwargs = dict(session_id="s1", title="批改", day=_DAY)
    append_pending(tmp_path, lines=["第一批"], **kwargs)
    path = append_pending(tmp_path, lines=["第二批"], **kwargs)

    body = path.read_text(encoding="utf-8")
    assert "第一批" in body
    assert "第二批" in body
    assert body.index("第一批") < body.index("第二批")


def test_title_is_sanitized(tmp_path) -> None:
    """标题里的路径分隔符不能逃出暂存目录。"""
    path = append_pending(
        tmp_path,
        session_id="s1",
        title="../../evil:name|with*bad?chars",
        lines=["x"],
        day=_DAY,
    )

    root = tmp_path / "logs"
    assert root in path.parents
    assert "/" not in path.name
    assert "\\" not in path.name
    assert ":" not in path.name
    assert "*" not in path.name
    assert "?" not in path.name


def test_blank_title_falls_back(tmp_path) -> None:
    path = append_pending(tmp_path, session_id="s1", title="   ", lines=["x"], day=_DAY)
    assert path.exists()


def test_list_pending_finds_all_oldest_first(tmp_path) -> None:
    append_pending(tmp_path, session_id="b", title="第二", lines=["x"], day=date(2026, 10, 16))
    append_pending(tmp_path, session_id="a", title="第一", lines=["x"], day=_DAY)

    found = list_pending(tmp_path)
    assert len(found) == 2
    assert found[0].name.startswith("a-")  # 更早的一天排前面
    assert found[1].name.startswith("b-")


def test_list_pending_empty_when_no_logs(tmp_path) -> None:
    assert list_pending(tmp_path) == []


def test_mark_done_removes_file_and_prunes_empty_day(tmp_path) -> None:
    path = append_pending(tmp_path, session_id="s1", title="t", lines=["x"], day=_DAY)
    assert path.exists()

    mark_done(path)
    assert not path.exists()
    # 那天没有别的文件了，空目录也收掉，不留一地空壳
    assert not path.parent.exists()


def test_mark_done_keeps_day_dir_when_others_remain(tmp_path) -> None:
    first = append_pending(tmp_path, session_id="s1", title="t", lines=["x"], day=_DAY)
    append_pending(tmp_path, session_id="s2", title="t2", lines=["y"], day=_DAY)

    mark_done(first)
    assert first.parent.exists()
    assert len(list_pending(tmp_path)) == 1


def test_pending_path_is_stable(tmp_path) -> None:
    """同一个会话同一天，路径稳定（追加才落在同一个文件里）。"""
    first = pending_path(tmp_path, session_id="s1", title="t", day=_DAY)
    second = pending_path(tmp_path, session_id="s1", title="t", day=_DAY)
    assert first == second


def test_pending_does_not_collide_with_memory_entries(tmp_path) -> None:
    """暂存落在 logs/ 子目录，不会被记忆条目的顶层扫描误当成一条记忆。"""
    append_pending(tmp_path, session_id="s1", title="t", lines=["x"], day=_DAY)
    assert list(tmp_path.glob("*.md")) == []
