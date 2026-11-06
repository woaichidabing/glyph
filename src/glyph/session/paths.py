"""会话落盘路径计算（纯函数，不读盘）。

全部是本模块的路径规则单点：

- 根目录：``~/.markr/sessions``，可用环境变量 ``GLYPH_SESSIONS_ROOT`` 覆盖（测试注入用）；
- 分层：``<root>/<YYYY>/<MM>/<DD>/session-<YYYYmmdd-HHMMSS>-<短 id>.jsonl``；
- 分层日期取自 **会话 id 里的时间戳**，因此「id → 路径」永远可反推，不需要索引。

本模块刻意不碰磁盘：想知道文件在不在由调用方负责（见 ``store.py``）。
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ..storage import data_root

GLYPH_SESSIONS_ROOT_ENV = "GLYPH_SESSIONS_ROOT"
"""会话根目录环境变量名。全项目只有 ``sessions_root()`` 读它（R1 配置单点）。"""

SESSION_ID_PREFIX = "session-"
SESSION_FILE_SUFFIX = ".jsonl"
SHORT_ID_LENGTH = 6  # secrets.token_hex(3) 的字符数

_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
_SESSION_ID_PATTERN = re.compile(
    rf"^{SESSION_ID_PREFIX}"
    r"(?P<date>\d{8})-(?P<time>\d{6})-"
    r"(?P<short_id>[0-9a-f]+)$"
)


def sessions_root() -> Path:
    """会话根目录：环境变量 ``GLYPH_SESSIONS_ROOT`` 优先，否则 ``~/.markr/sessions``。"""
    raw = os.environ.get(GLYPH_SESSIONS_ROOT_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_root() / "sessions"


def local_now() -> datetime:
    """当前本地时间（带时区）。所有落盘时间都经它，测试可替换（R6 可注入）。"""
    return datetime.now().astimezone()


def generate_session_id(now: datetime, *, short_id: str | None = None) -> str:
    """生成会话 id：``session-<YYYYmmdd-HHMMSS>-<6 位小写十六进制>``。

    ``short_id`` 缺省时用 ``secrets.token_hex(3)``，因此同一秒连开多个会话也不撞名。
    """
    token = short_id if short_id is not None else secrets.token_hex(SHORT_ID_LENGTH // 2)
    return f"{SESSION_ID_PREFIX}{now.strftime(_TIMESTAMP_FORMAT)}-{token}"


def session_id_timestamp(session_id: str) -> datetime | None:
    """从会话 id 里解出时间戳；id 不合规范返回 None（例如手工构造的老 id）。"""
    matched = _SESSION_ID_PATTERN.match(session_id)
    if matched is None:
        return None
    try:
        return datetime.strptime(f"{matched['date']}{matched['time']}", "%Y%m%d%H%M%S")
    except ValueError:
        # 形如 20261332-999999 的假日期：形状对但不是合法时间
        return None


def is_valid_session_id(session_id: str) -> bool:
    """这个 id 是不是本项目自己生成的形状（``session-<日期>-<时间>-<短id>``）。

    任何**从外部进来**的会话 id（URL 路径、请求体）都要先过这一关再拼路径：
    ``session_path`` 会把 id 原样拼进文件名，形如 ``../../../../evil`` 的 id 能跳出
    会话根目录，删到 / 读到不该碰的文件。校验放在入口，不放进 ``session_path`` 里，
    是为了让内部按 id 拼路径的既有调用不受影响。
    """
    return _SESSION_ID_PATTERN.match(session_id) is not None


def session_path(
    session_id: str,
    now: datetime | None = None,
    *,
    root: Path | None = None,
) -> Path:
    """拼出完整路径：``root/<YYYY>/<MM>/<DD>/<session_id>.jsonl``。

    分层日期取自 ``session_id`` 里的时间戳；``now`` 只在 id 不合规范、解不出时间戳时兜底
    （保证任何 id 都能算出唯一确定的路径）。
    """
    stamp = session_id_timestamp(session_id) or now or local_now()
    base = sessions_root() if root is None else root
    return (
        base / f"{stamp:%Y}" / f"{stamp:%m}" / f"{stamp:%d}" / f"{session_id}{SESSION_FILE_SUFFIX}"
    )


def parse_session_id(path: Path) -> str:
    """从文件路径反解会话 id（去目录、去 ``.jsonl`` 后缀）。"""
    if path.suffix == SESSION_FILE_SUFFIX:
        return path.stem
    return path.name


def iter_session_paths(root: Path | None = None) -> Iterator[Path]:
    """按「年/月/日目录倒序 + 文件名倒序」遍历全部会话文件（最新在前）。

    不建索引：目录名与文件名都是零填充的时间串，字典序即时间序（D15）。
    根目录不存在时安静返回空迭代器 —— 第一次用就是这种情形。
    """
    base = sessions_root() if root is None else root
    if not base.is_dir():
        return
    for year_dir in _subdirs_desc(base):
        for month_dir in _subdirs_desc(year_dir):
            for day_dir in _subdirs_desc(month_dir):
                files = [
                    p for p in day_dir.iterdir() if p.is_file() and p.suffix == SESSION_FILE_SUFFIX
                ]
                yield from sorted(files, key=lambda p: p.name, reverse=True)


def _subdirs_desc(directory: Path) -> list[Path]:
    """子目录按名字倒序（目录名是零填充数字，字典序即数值序）。"""
    return sorted(
        (p for p in directory.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
    )
