"""会话层统一错误类型（铁律 R5）。

对外只抛这一族异常：

- ``SessionNotFoundError``：按 id 打开，但文件不存在；
- ``SessionCorruptError``：文件整体不可读（首行不是 session_meta）；
- ``SessionWriteError``：追加写失败（磁盘满、权限、会话已关闭等），**不静默吞**；
- ``SessionError``：以上三者的基类，调用方可用一句 ``except SessionError`` 兜住全部。

基类 ``GlyphError`` 放在顶层 ``glyph.errors``（它不依赖任何项目内模块），session 只向它
挂载自己的错误族，**不反向 import runtime**；依赖方向始终单向（session / runtime 都只指向
顶层的 ``glyph.errors`` / ``glyph.types``）。
"""

from __future__ import annotations

from ..errors import GlyphError


class SessionError(GlyphError):
    """会话层错误基类。消息一律写成能直接给人看的话（含会话 id / 路径）。"""


class SessionNotFoundError(SessionError):
    """按 id 打开会话，但对应的 JSONL 文件不存在。"""


class SessionCorruptError(SessionError):
    """会话文件整体不可解析：没有首行 session_meta，或首行读不出元信息。"""


class SessionWriteError(SessionError):
    """写入会话流水失败。失败必须冒泡，不许静默吞掉导致「会话悄悄少几行」。"""
