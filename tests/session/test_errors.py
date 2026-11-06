"""session/errors.py 测试：统一错误类型（铁律 R5）。

会话层对外只抛这一族异常，调用方可以一句 ``except SessionError`` 兜住全部。
"""

from __future__ import annotations

from glyph.errors import GlyphError
from glyph.session.errors import (
    SessionCorruptError,
    SessionError,
    SessionNotFoundError,
    SessionWriteError,
)


def test_session_errors_all_derive_from_markr_error() -> None:
    """四个类型同族：既能单独捕获，也能被 GlyphError 统一兜住。"""
    for error_type in (SessionError, SessionNotFoundError, SessionCorruptError, SessionWriteError):
        assert issubclass(error_type, GlyphError)


def test_specific_errors_derive_from_session_error() -> None:
    """细分类型都是 SessionError 的子类，方便调用方按粒度捕获。"""
    assert issubclass(SessionNotFoundError, SessionError)
    assert issubclass(SessionCorruptError, SessionError)
    assert issubclass(SessionWriteError, SessionError)


def test_errors_carry_human_readable_message() -> None:
    """消息要能直接给人看（含会话 id / 路径），不向前端甩 traceback。"""
    error = SessionNotFoundError("会话不存在：session-20260919-175003-8f3c2a")

    assert "session-20260919-175003-8f3c2a" in str(error)
