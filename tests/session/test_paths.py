"""session/paths.py 测试：纯路径计算，不读盘。

切片 1：会话根目录取值优先级（环境变量 > ~/.markr/sessions）。
切片 2：会话 id 生成（时间戳 + 短 id，同秒不撞）。
切片 3：id → 年/月/日分层路径，且分层只认 id 里的时间戳。
切片 4：路径反解 id 往返、遍历顺序 newest-first。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from glyph.session import paths
from glyph.storage import data_root

_TZ = timezone(timedelta(hours=8))
_NOW = datetime(2026, 9, 19, 17, 50, 3, tzinfo=_TZ)
_SESSION_ID = "session-20260919-175003-8f3c2a"


# ---------- 切片 1：会话根目录 ----------


def test_sessions_root_defaults_to_markr_home(monkeypatch) -> None:
    """没设环境变量时，默认落在 ~/.markr/sessions。"""
    monkeypatch.delenv(paths.GLYPH_SESSIONS_ROOT_ENV, raising=False)

    assert paths.sessions_root() == data_root() / "sessions"


def test_sessions_root_prefers_environment_variable(monkeypatch, tmp_path) -> None:
    """环境变量优先：测试注入靠它，生产走默认值。"""
    injected = tmp_path / "sessions"
    monkeypatch.setenv(paths.GLYPH_SESSIONS_ROOT_ENV, str(injected))

    assert paths.sessions_root() == injected


# ---------- 切片 2：会话 id ----------


def test_generate_session_id_contains_timestamp_and_short_id() -> None:
    """id 形状固定：session-<YYYYmmdd-HHMMSS>-<6 位十六进制>。"""
    session_id = paths.generate_session_id(_NOW, short_id="abcdef")

    assert session_id == "session-20260919-175003-abcdef"


def test_generate_session_id_is_unique_within_the_same_second() -> None:
    """同一秒连开多个会话也不撞名。"""
    session_ids = {paths.generate_session_id(_NOW) for _ in range(32)}

    assert len(session_ids) == 32
    for session_id in session_ids:
        _, date_part, time_part, short_id = session_id.split("-")
        assert (date_part, time_part) == ("20260919", "175003")
        assert len(short_id) == 6
        assert set(short_id) <= set("0123456789abcdef")


# ---------- 切片 3：分层路径 ----------


def test_session_path_is_layered_by_year_month_day(tmp_path) -> None:
    """root/<YYYY>/<MM>/<DD>/<session_id>.jsonl 。"""
    path = paths.session_path(_SESSION_ID, root=tmp_path)

    assert path == tmp_path / "2026" / "09" / "19" / f"{_SESSION_ID}.jsonl"


def test_session_path_takes_date_from_session_id_not_from_now(tmp_path) -> None:
    """分层日期只认 id 里的时间戳，保证「id → 路径」永远可反推。"""
    other_now = datetime(2020, 1, 2, 3, 4, 5, tzinfo=_TZ)

    path = paths.session_path(_SESSION_ID, other_now, root=tmp_path)

    assert [p.name for p in path.parents[:3]] == ["19", "09", "2026"]


# ---------- 切片 4：反解与遍历 ----------


def test_parse_session_id_round_trips(tmp_path) -> None:
    """路径 → id 与 id → 路径互逆。"""
    path = paths.session_path(_SESSION_ID, root=tmp_path)

    assert paths.parse_session_id(path) == _SESSION_ID


def test_iter_session_paths_returns_newest_first(tmp_path) -> None:
    """先按年/月/日目录倒序，同一天内再按文件名倒序（不做索引，直接扫目录）。"""
    files = [
        tmp_path / "2026" / "08" / "31" / "session-20260831-090000-aaaaaa.jsonl",
        tmp_path / "2026" / "09" / "19" / "session-20260919-090000-cccccc.jsonl",
        tmp_path / "2026" / "09" / "19" / "session-20260919-175003-bbbbbb.jsonl",
        tmp_path / "2026" / "10" / "01" / "session-20261001-090000-dddddd.jsonl",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    listed = list(paths.iter_session_paths(tmp_path))

    assert listed == [
        files[3],  # 2026/10/01
        files[2],  # 2026/09/19 17:50:03
        files[1],  # 2026/09/19 09:00:00
        files[0],  # 2026/08/31
    ]


def test_iter_session_paths_on_missing_root_yields_nothing(tmp_path) -> None:
    """根目录还不存在（第一次用）时不得炸，返回空。"""
    assert list(paths.iter_session_paths(tmp_path / "not-created-yet")) == []


def test_session_errors_hang_off_top_level_markr_error() -> None:
    """§1.1 验收：会话错误族挂在顶层 glyph.errors.GlyphError 上，session 不反向依赖 runtime。"""
    from glyph.errors import GlyphError
    from glyph.session import errors as session_errors

    assert issubclass(session_errors.SessionError, GlyphError)
