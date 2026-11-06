"""暂存区（D29）：提炼产出的原始材料先落这里，等整理器合并。

一个会话一个文件、只追加、带标题——照 CC 的 logs/YYYY/MM/DD/<id>-<title>.md。
落在 <root>/logs/ 子目录，与 reviews/、.trash/ 同一隔离思路：
条目扫描只 glob 顶层 *.md，暂存天然不会被当成一条记忆。

**用户看不见暂存**——它是内部管道，不是给人读的记忆。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

__all__ = [
    "PENDING_DIR_NAME",
    "pending_path",
    "append_pending",
    "list_pending",
    "mark_done",
]

PENDING_DIR_NAME = "logs"

# 文件名里不允许出现的东西：路径分隔符、Windows 保留字符、控制字符。
# 标题来自会话首条消息，可能含任意字符，必须清洗（R9：中文/全角路径本就脆）。
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_TITLE_CHARS = 40
_MAX_SESSION_CHARS = 40


def _safe(text: str, *, fallback: str, max_chars: int) -> str:
    """清洗成安全的文件名片段：去分隔符、压空白、限长；空则用 fallback。"""
    cleaned = _UNSAFE.sub("", (text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return fallback
    return cleaned[:max_chars]


def pending_path(root: Path, *, session_id: str, title: str, day: date) -> Path:
    """暂存文件路径：<root>/logs/YYYY/MM/DD/<会话id>-<标题>.md。

    同一会话同一天路径稳定，追加才落在同一个文件里。
    """
    safe_session = _safe(session_id, fallback="session", max_chars=_MAX_SESSION_CHARS)
    safe_title = _safe(title, fallback="未命名", max_chars=_MAX_TITLE_CHARS)
    return (
        root
        / PENDING_DIR_NAME
        / f"{day.year:04d}"
        / f"{day.month:02d}"
        / f"{day.day:02d}"
        / f"{safe_session}-{safe_title}.md"
    )


def append_pending(
    root: Path,
    *,
    session_id: str,
    title: str,
    lines: list[str],
    day: date,
) -> Path:
    """把一批材料追加进当天的暂存文件，返回文件路径。

    只追加（mode "a"），绝不覆盖——同一个会话当天分几批提炼都能留住。
    """
    path = pending_path(root, session_id=session_id, title=title, day=day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(lines)
    if payload and not payload.endswith("\n"):
        payload += "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
    return path


def list_pending(root: Path) -> list[Path]:
    """列出所有还没被整理掉的暂存文件，旧的排前面（按日期目录名即可）。

    没有暂存时返回空列表（不返回 None）——调用方按"空即无事可做"处理。
    """
    base = root / PENDING_DIR_NAME
    if not base.is_dir():
        return []
    found: list[Path] = []
    # 按年月日目录顺序遍历，天然就是时间序（目录名定长零填充）
    for year in sorted(p for p in base.iterdir() if p.is_dir()):
        for month in sorted(p for p in year.iterdir() if p.is_dir()):
            for day in sorted(p for p in month.iterdir() if p.is_dir()):
                found.extend(sorted(p for p in day.iterdir() if p.is_file()))
    return found


def mark_done(path: Path) -> None:
    """整理完毕，把这份暂存收走；当天没有别的文件了就连空目录一起清掉。

    只删这一个文件——不做递归删除，免得路径算错时误伤。
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return
    # 自下而上收掉空目录；只收 logs/ 以内的三层，不碰根
    parent = path.parent
    for _ in range(3):  # 日 / 月 / 年，三层
        try:
            if any(parent.iterdir()):
                break
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
