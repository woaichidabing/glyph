"""用户数据总目录。

所有学习数据（会话、记忆、生词本、背词、计划、日志、凭据、模型配置）都从
data_root() 派生。默认：

- 新用户：~/Documents/Glyph
- 已有 ~/.markr 的老用户：继续用它，避免数据消失
- 设置里换过位置：读 ~/.markr/data-root.json 指针

换位置只改指针，不删除旧数据。迁移由显式操作完成。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

__all__ = [
    "POINTER_NAME",
    "data_root",
    "default_data_root",
    "legacy_data_root",
    "pointer_path",
    "set_data_root",
    "usage",
    "session_count",
    "migrate_data",
]

POINTER_NAME = "data-root.json"
DEFAULT_DIR_NAME = "Glyph"


def pointer_path() -> Path:
    """固定的小指针文件；它本身永远在用户目录，不随数据盘走。"""
    return Path.home() / ".markr" / POINTER_NAME


def default_data_root() -> Path:
    return Path.home() / "Documents" / DEFAULT_DIR_NAME


def legacy_data_root() -> Path:
    return Path.home() / ".markr"


def _read_pointer() -> Path | None:
    try:
        raw = json.loads(pointer_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    root = raw.get("root")
    if not isinstance(root, str) or not root.strip():
        return None
    candidate = Path(root).expanduser()
    return candidate if candidate.is_absolute() else None


def data_root() -> Path:
    raw = os.environ.get("GLYPH_DATA_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser()
    pointer = _read_pointer()
    if pointer is not None:
        return pointer
    legacy = legacy_data_root()
    if legacy.exists() and any(legacy.iterdir()):
        return legacy
    return default_data_root()


def set_data_root(path: str | Path) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError("数据目录必须是绝对路径。")
    target = target.resolve()
    if target.exists() and not target.is_dir():
        raise ValueError("这个路径不是文件夹。")
    target.mkdir(parents=True, exist_ok=True)
    pointer_path().parent.mkdir(parents=True, exist_ok=True)
    tmp = pointer_path().with_name(pointer_path().name + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "root": str(target)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, pointer_path())
    return target


def _iter_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not Path(current, d).is_symlink()]
        for name in files:
            path = Path(current, name)
            if path.is_file() and not path.is_symlink():
                yield path


def usage(root: Path | None = None) -> int:
    base = root or data_root()
    if not base.exists():
        return 0
    total = 0
    for path in _iter_files(base):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def session_count(root: Path | None = None) -> int:
    sessions = (root or data_root()) / "sessions"
    if not sessions.exists():
        return 0
    return sum(1 for path in _iter_files(sessions) if path.suffix == ".jsonl")


def migrate_data(source: Path, target: Path) -> int:
    """把 source 里的文件复制到 target；不删 source，不覆盖冲突文件。"""
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source == target:
        return 0
    if source in target.parents or target in source.parents:
        raise ValueError("新旧目录不能互相包含。")
    if not source.exists():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for current, dirs, files in os.walk(source):
        rel = Path(current).relative_to(source)
        dst_dir = target / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        dirs[:] = [d for d in dirs if not Path(current, d).is_symlink()]
        for name in files:
            src = Path(current, name)
            if src.is_symlink() or not src.is_file():
                continue
            if rel == Path(".") and name == POINTER_NAME:
                continue
            dst = dst_dir / name
            if dst.exists():
                if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                    continue
                raise ValueError(f"目标目录已有同名文件：{dst}")
            shutil.copy2(src, dst)
            copied += 1
    return copied
