"""Glyph 会话短期记忆层。

一次会话 = 一个 JSONL 文件的事实流水：只追加、不改写、可重建。
对外只有五个口子：``SessionHistory`` 对象、``new_session`` / ``open_session``
两个工厂、本模块的异常族，外加 ``replay`` 回放视图（给界面回灌历史用，
工具结果也在里面）。

- 用法：``await new_session(system_prompt=..., model=..., cwd=...)`` 开新会话；
  ``await open_session(session_id, system_prompt=...)`` 回到上次那一页。
- 上层（AgentLoop）拿到的 ``SessionHistory`` **只用不操心**：要发什么问
  ``messages()``，发生了什么调 ``record_*()``，结束时 ``close()``。

本包不持有长期记忆的内容，只在 ``messages()`` 末尾提供一个**上下文注入插槽**
（日期 / 记忆目录等片段由装配根传入，只活在返回值里、不落盘）；上下文压缩、
索引也不在本包范围内。
"""

from __future__ import annotations

from .errors import SessionCorruptError, SessionError, SessionNotFoundError, SessionWriteError
from .history import SessionHistory, new_session, open_session
from .replay import ReplayItem, build_replay

__all__ = [
    "ReplayItem",
    "SessionCorruptError",
    "SessionError",
    "SessionHistory",
    "SessionNotFoundError",
    "SessionWriteError",
    "build_replay",
    "new_session",
    "open_session",
]
