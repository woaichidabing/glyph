"""系统提示词的版本管理（给人和 AI 迭代用，不做界面）。

为什么要它：以前改提示词是直接改代码常量，改一版上一版就没了，没法回滚、没法对比。
这里把**静态文案段**存成有编号的 markdown 文件，随时新建 / 切换 / 对比；
「当前能力」与「工具清单」两段**不进版本文件**——它们必须随装配实时生成。

目录（可用 GLYPH_PROMPTS 覆盖）：

    ~/.markr/prompts/
      index.json          # 各版元信息 + 当前生效哪一版
      versions/v1.md      # 一版一个文件：`## 段名` 分隔

命令行（等价于 ``python -m glyph.prompt_store <子命令>``）：

    list / show <id> / new <名字> [--note 备注] / use <id> / diff <a> <b> / export <id> <路径>
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .storage import data_root

__all__ = [
    "PromptVersion",
    "prompts_dir",
    "ensure_seeded",
    "list_versions",
    "active_id",
    "set_active",
    "read_version",
    "create_version",
    "load_active_sections",
    "parse_sections",
    "render_sections",
    "validate_text",
]


def prompts_dir() -> Path:
    raw = os.environ.get("GLYPH_PROMPTS", "").strip()
    return Path(raw).expanduser() if raw else data_root() / "prompts"


def _index_path() -> Path:
    return prompts_dir() / "index.json"


def _versions_dir() -> Path:
    return prompts_dir() / "versions"


@dataclass(frozen=True)
class PromptVersion:
    id: str
    name: str
    note: str
    created_at: str


def _read_index() -> dict:
    """读索引。文件不在 = 还没建过（正常）；读不出来/坏了 = 不静默重建。

    坏索引直接重建会**覆盖已有版本**（并让用户以为版本丢了），所以这里先把坏文件
    改名备份，再抛 PromptStoreError 交给调用方决定——装配路径捕获它、回落内置。
    """
    path = _index_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "active": "", "items": []}
    except (OSError, UnicodeError, ValueError) as exc:
        raise PromptStoreError(f"提示词索引读不出来（{path}）：{exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        _quarantine(path, "corrupt")
        raise PromptStoreError(f"提示词索引不是合法 JSON（{path}）：{exc}") from exc
    if not isinstance(data, dict):
        return {"version": 1, "active": "", "items": []}
    data.setdefault("items", [])
    data.setdefault("active", "")
    return data


class PromptStoreError(RuntimeError):
    """版本文件/索引读不出来。装配路径要捕获它并回落内置，别让对话挂掉。"""


def _quarantine(path: Path, tag: str) -> None:
    """把坏文件改名留档（不删），并给唯一后缀，避免再次覆盖。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        path.rename(path.with_name(path.name + f".{tag}-{stamp}"))
    except OSError:
        pass


def _write_index(data: dict) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def list_versions() -> list[PromptVersion]:
    items = _read_index().get("items") or []  # 坏索引由 CLI 捕获并提示
    out: list[PromptVersion] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            PromptVersion(
                id=str(item.get("id") or ""),
                name=str(item.get("name") or ""),
                note=str(item.get("note") or ""),
                created_at=str(item.get("created_at") or ""),
            )
        )
    return out


def active_id() -> str:
    return str(_read_index().get("active") or "")


def set_active(version_id: str) -> None:
    data = _read_index()
    known = {str(i.get("id")) for i in data.get("items") or [] if isinstance(i, dict)}
    if version_id not in known:
        raise ValueError(f"没有这一版：{version_id}")
    data["active"] = version_id
    _write_index(data)


def _version_path(version_id: str) -> Path:
    return _versions_dir() / f"{version_id}.md"


def read_version(version_id: str) -> str:
    return _version_path(version_id).read_text(encoding="utf-8")


def render_sections(sections: list[tuple[str, str]]) -> str:
    """段列表 → 一份 markdown 文件。"""
    parts = ["<!-- 一段一个 `## 段名`；文件名即版本号。改动请保留段名。 -->", ""]
    for name, body in sections:
        parts.append(f"## {name}")
        parts.append("")
        parts.append(body.strip())
        parts.append("")
    return "\n".join(parts)


def parse_sections(text: str, known: list[str] | None = None) -> list[tuple[str, str]]:
    """markdown 文件 → 段列表。

    只有 **已知段名**（known）的「## xxx」才算分隔线：提示词正文里本来就有
    「## 你的口吻：自然」这类小标题，不能把它当成新段的开头（否则一段会被切断）。
    """
    allowed = set(known) if known else None
    sections: list[tuple[str, str]] = []
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        name = line[3:].strip() if line.startswith("## ") else ""
        if name and (allowed is None or name in allowed):
            if current is not None:
                sections.append((current, "\n".join(buffer).strip()))
            current = name
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        sections.append((current, "\n".join(buffer).strip()))
    return [(n, b) for n, b in sections if n]


def validate_text(text: str, required: list[str]) -> list[str]:
    """检查一份版本文件：缺哪些段、有没有空段。返回问题列表（空 = 合格）。"""
    parsed = dict(parse_sections(text, required))
    problems = [f"缺少段落：{name}" for name in required if name not in parsed]
    problems += [f"段落是空的：{name}" for name, body in parsed.items() if not body.strip()]
    return problems


def ensure_seeded(default_sections: list[tuple[str, str]]) -> str:
    """第一次使用时把内置那份存成 v1（幂等）；返回当前生效的版本号。"""
    data = _read_index()
    if data.get("items"):
        return active_id()
    _versions_dir().mkdir(parents=True, exist_ok=True)
    # 已有 v1.md 就别覆盖：索引丢了不代表版本文件没了，重建会把用户改过的 v1 冲掉。
    v1 = _version_path("v1")
    if not v1.exists():
        v1.write_text(render_sections(default_sections), encoding="utf-8")
    data = {
        "version": 1,
        "active": "v1",
        "items": [
            {
                "id": "v1",
                "name": "默认版",
                "note": "首次建档：把代码里的提示词原样存一份",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        ],
    }
    _write_index(data)
    return "v1"


def create_version(
    name: str,
    note: str = "",
    from_id: str | None = None,
    default_sections: list[tuple[str, str]] | None = None,
) -> str:
    """新建一版：默认从当前生效版复制；没有存量就以内置那份为底。"""
    data = _read_index()
    ids = [str(i.get("id")) for i in data.get("items") or [] if isinstance(i, dict)]
    n = 1
    while f"v{n}" in ids:
        n += 1
    new_id = f"v{n}"
    source = from_id or active_id()
    try:
        body = read_version(source) if source else render_sections(default_sections or [])
    except OSError:
        body = render_sections(default_sections or [])
    _versions_dir().mkdir(parents=True, exist_ok=True)
    _version_path(new_id).write_text(body, encoding="utf-8")
    data.setdefault("items", []).append(
        {
            "id": new_id,
            "name": name or new_id,
            "note": note,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    )
    _write_index(data)
    return new_id


def load_active_sections(required: list[str]) -> list[tuple[str, str]] | None:
    """装配时取当前版本；没有 / 坏了 / 缺段 → None（调用方用内置那份）。"""
    try:
        version = active_id()
    except PromptStoreError:
        return None
    if not version:
        return None
    try:
        text = read_version(version)
    except (OSError, UnicodeError, ValueError):
        return None
    if validate_text(text, required):
        return None
    parsed = dict(parse_sections(text, required))
    return [(name, parsed[name]) for name in required]



def _main(argv: list[str]) -> int:
    """命令行：list / show / new / use / diff / export / validate。"""
    import difflib

    from .agent_prompt import STATIC_SECTIONS

    required = [name for name, _ in STATIC_SECTIONS]
    ensure_seeded(STATIC_SECTIONS)
    args = list(argv)
    cmd = args[0] if args else "list"

    if cmd == "list":
        current = active_id()
        for item in list_versions():
            mark = "  <- 当前" if item.id == current else ""
            note = f" · {item.note}" if item.note else ""
            print(f"{item.id:4s} {item.name}{note}  ({item.created_at}){mark}")
        return 0

    if cmd == "show":
        if len(args) < 2:
            print("用法：show <id>")
            return 2
        print(read_version(args[1]))
        return 0

    if cmd == "new":
        if len(args) < 2:
            print("用法：new <名字> [--note 备注]")
            return 2
        note = ""
        if "--note" in args:
            idx = args.index("--note")
            note = " ".join(args[idx + 1:])
        new_id = create_version(args[1], note=note, default_sections=STATIC_SECTIONS)
        print(f"已新建 {new_id}")
        print(f"改这个文件：{_version_path(new_id)}")
        return 0

    if cmd == "use":
        if len(args) < 2:
            print("用法：use <id>")
            return 2
        problems = validate_text(read_version(args[1]), required)
        if problems:
            print("这一版有问题，没切：")
            for p in problems:
                print("  - " + p)
            return 1
        set_active(args[1])
        print(f"已切到 {args[1]}（下一轮对话生效）")
        return 0

    if cmd == "diff":
        if len(args) < 3:
            print("用法：diff <a> <b>")
            return 2
        a = read_version(args[1]).splitlines()
        b = read_version(args[2]).splitlines()
        for line in difflib.unified_diff(a, b, fromfile=args[1], tofile=args[2], lineterm=""):
            print(line)
        return 0

    if cmd == "export":
        if len(args) < 3:
            print("用法：export <id> <路径>")
            return 2
        shutil.copyfile(_version_path(args[1]), args[2])
        print(f"已导出到 {args[2]}")
        return 0

    if cmd == "validate":
        target = args[1] if len(args) > 1 else active_id()
        problems = validate_text(read_version(target), required)
        if not problems:
            print(f"{target} 校验通过")
            return 0
        for p in problems:
            print("  - " + p)
        return 1

    print(
        "用法：list | show <id> | new <名字> [--note 备注] | use <id> | "
        "diff <a> <b> | export <id> <路径> | validate [id]"
    )
    return 2


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(_main(_sys.argv[1:]))
