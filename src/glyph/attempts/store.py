"""AttemptStore：尝试记录的本地文件存储（T07，同构 MemoryStore）。

布局：根目录平铺 ``attempt-<内容id12>.md``；删除移到 ``.trash/``（软删、可追溯、
绝不硬删）；写入走临时文件 + ``os.replace`` 原子替换。frontmatter 存单行标量，
正文 ``##`` 分节存题目 / 作答 / 依据 / 用户确认原话（复用 memory 的 body 渲染）。

三条 T07 特有保证：

1. **幂等**：``attempt_id`` 内容派生，同一条作答重复 ``save`` 命中已存在文件，
   直接返回 ``exists`` 不重写，首次 ``confirmed_at`` 保留——重试 / 重复点击不产生
   重复记录；
2. **读取有限**：``list`` 支持知识点相关性过滤（泛主题 ↔ 带修饰的具体点）、确认时间
   倒序、条数上限、总字节预算，绝不把全历史塞回上下文；
3. **坏文件不炸场**：解析失败的条目跳过并计数，好记录照常返回。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..memory.body import parse_body_sections
from ..memory.frontmatter import render_frontmatter
from ..memory.slug import normalize_fragment
from ..session.paths import local_now
from .errors import AttemptNotFoundError, AttemptStoreError
from .paths import attempt_path, attempts_root, is_valid_attempt_id, trash_path
from .types import ATTEMPTS_VERSION, AttemptRecord, Outcome

__all__ = ["AttemptPage", "AttemptStore", "DEFAULT_READ_LIMIT", "DEFAULT_TOTAL_MAX_BYTES"]

DEFAULT_READ_LIMIT = 5
# read_attempts 一次回灌的总字节预算（对齐记忆目录 32KiB 的思路，单工具更紧）：
# 一次只看最近几条，超了就截断并告知模型还有更早的，不把上下文撑爆。
DEFAULT_TOTAL_MAX_BYTES = 8 * 1024

# 正文四个固定分节标题（中文，render_body / parse_body_sections 以此为 key）。
_SECTION_PROMPT = "题目"
_SECTION_ANSWER = "你的作答"
_SECTION_EVIDENCE = "判定依据"
_SECTION_CONFIRMATION = "用户确认"


@dataclass(frozen=True)
class AttemptPage:
    """一次有限读取的结果：当前页记录、是否被上限 / 预算截断、跳过的坏文件数。"""

    records: list[AttemptRecord]
    truncated: bool
    skipped_files: int


# 知识点是自由文本：record 时常被写得更具体（带括号 / 英文修饰，如「一般过去时
# （不规则动词 go → went）」），read 时常用泛主题（「一般过去时」）。精确相等会让
# 跨会话召回漏掉刚记的点（真机 BUG-3）。相等优先；双方都足够"实"（≥3 字符）时
# 允许短串作为长串的子串命中；过短片段（如「过去」）不做子串，避免泛字误伤。
_SKILL_MIN_MATCH_CHARS = 3


def _skill_matches(recorded: str, query: str) -> bool:
    """知识点相关性：归一化后相等，或较短一方（≥3 字符）是较长一方的子串。"""
    recorded_n = normalize_fragment(recorded)
    query_n = normalize_fragment(query)
    if not recorded_n or not query_n:
        return False
    if recorded_n == query_n:
        return True
    short, long_ = (
        (recorded_n, query_n) if len(recorded_n) <= len(query_n) else (query_n, recorded_n)
    )
    return len(short) >= _SKILL_MIN_MATCH_CHARS and short in long_


class AttemptStore:
    def __init__(self, root: Path | str | None = None, *, clock=local_now) -> None:
        self.root = Path(root) if root is not None else attempts_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.skipped_files = 0

    # ------------------------------------------------------------------ 写入

    def save(self, record: AttemptRecord) -> tuple[str, AttemptRecord]:
        """幂等写入。返回 (``created`` | ``exists``, 落盘后的记录)。

        同 id 已存在说明是重试 / 重复提交：不重写、不改首次确认时间，原样返回磁盘版。
        """
        target = attempt_path(self.root, record.attempt_id)
        if target.exists():
            existing = self._read_file(target)
            if existing is None:
                # 同名文件在却坏了：不能静默覆盖（可能抹掉可追溯数据），交上层处理
                raise AttemptStoreError(f"尝试记录已存在但内容损坏：{record.attempt_id}")
            return "exists", existing

        confirmed = replace(record, confirmed_at=self._clock())
        self._atomic_write(target, self._render(confirmed))
        return "created", confirmed

    def soft_delete(self, attempt_id: str) -> Path:
        """按 id 软删到 .trash；不存在 / id 非法抛 AttemptNotFoundError。"""
        self.get(attempt_id)  # 先确认活条目真实存在（坏文件同样不允许静默删）
        dest = trash_path(self.root, attempt_id, self._clock())
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(attempt_path(self.root, attempt_id), dest)
        return dest

    # ------------------------------------------------------------------ 读取

    def get(self, attempt_id: str) -> AttemptRecord:
        if not is_valid_attempt_id(attempt_id):
            raise AttemptNotFoundError(f"尝试记录不存在：{attempt_id}")
        path = attempt_path(self.root, attempt_id)
        if not path.exists():
            raise AttemptNotFoundError(f"尝试记录不存在：{attempt_id}")
        rec = self._read_file(path)
        if rec is None:
            raise AttemptStoreError(f"尝试记录内容损坏：{attempt_id}")
        return rec

    def list(
        self,
        skill: str | None = None,
        *,
        limit: int = DEFAULT_READ_LIMIT,
        total_max_bytes: int = DEFAULT_TOTAL_MAX_BYTES,
    ) -> AttemptPage:
        """按知识点（可选，相关性匹配）+ 最近确认时间倒序读有限记录。

        知识点匹配见 ``_skill_matches``：归一化相等，或较短一方（≥3 字符）是较长
        一方子串（泛主题 ↔ 带修饰的具体点互相命中）。先按条数 ``limit`` 取最近的，
        再按 ``total_max_bytes`` 逐条装，任一上限触顶都把 ``truncated`` 置真。坏文件
        跳过计数。
        """
        records: list[AttemptRecord] = []
        skipped = 0
        for path in sorted(self.root.glob("attempt-*.md")):
            rec = self._read_file(path)
            if rec is None:
                skipped += 1
                continue
            records.append(rec)

        if skill is not None:
            records = [r for r in records if _skill_matches(r.skill, skill)]

        records.sort(key=lambda r: r.confirmed_at or r.created_at, reverse=True)

        limit_hit = len(records) > limit
        page = records[:limit]

        kept: list[AttemptRecord] = []
        used = 0
        budget_cut = False
        for rec in page:
            size = self._payload_bytes(rec)
            # 第一条即使超预算也保留（否则最近一条都看不到），从第二条起裁
            if kept and used + size > total_max_bytes:
                budget_cut = True
                break
            kept.append(rec)
            used += size

        return AttemptPage(
            records=kept,
            truncated=limit_hit or budget_cut,
            skipped_files=skipped,
        )

    # -------------------------------------------------------------- 序列化内部

    @staticmethod
    def _payload_bytes(rec: AttemptRecord) -> int:
        """估算一条记录回灌给模型的精简表示字节（不含确认原话 / session_id）。"""
        when = (rec.confirmed_at or rec.created_at).isoformat()
        text = "\n".join([rec.skill, rec.outcome.value, when, rec.prompt, rec.answer, rec.evidence])
        return len(text.encode("utf-8"))

    @staticmethod
    def _render(rec: AttemptRecord) -> str:
        metadata = {
            "version": str(ATTEMPTS_VERSION),
            "attempt_id": rec.attempt_id,
            "session_id": rec.session_id,
            "skill": rec.skill,
            "hint_used": "true" if rec.hint_used else "false",
            "outcome": rec.outcome.value,
            "created_at": rec.created_at.isoformat(),
            "confirmed_at": rec.confirmed_at.isoformat() if rec.confirmed_at else "",
        }
        sections = [
            (_SECTION_PROMPT, rec.prompt),
            (_SECTION_ANSWER, rec.answer),
            (_SECTION_EVIDENCE, rec.evidence),
            (_SECTION_CONFIRMATION, rec.confirmation),
        ]
        return render_frontmatter(metadata) + "\n" + AttemptStore._render_sections(sections) + "\n"

    @staticmethod
    def _render_sections(sections: list[tuple[str, str]]) -> str:
        """渲染固定的 ``## 标题`` + 引用块正文（形状与 parse_body_sections 对齐）。

        长文本逐行加 ``> ``，既和记忆正文风格一致，也避免内容里的行被误读成新小节。
        """
        blocks: list[str] = []
        for title, text in sections:
            quoted = "\n".join("> " + line if line.strip() else ">" for line in text.split("\n"))
            blocks.append(f"## {title}\n{quoted}")
        return "\n\n".join(blocks)

    @staticmethod
    def _parse(text: str) -> AttemptRecord | None:
        try:
            split = AttemptStore._split_frontmatter(text)
            if split is None:
                return None
            meta, body = split
            sections = parse_body_sections(body)
            attempt_id = meta.get("attempt_id", "")
            if not is_valid_attempt_id(attempt_id):
                return None
            return AttemptRecord(
                attempt_id=attempt_id,
                session_id=meta["session_id"],
                skill=meta["skill"],
                prompt=sections.get(_SECTION_PROMPT, ""),
                answer=sections.get(_SECTION_ANSWER, ""),
                hint_used=meta.get("hint_used") == "true",
                outcome=Outcome(meta["outcome"]),
                evidence=sections.get(_SECTION_EVIDENCE, ""),
                confirmation=sections.get(_SECTION_CONFIRMATION, ""),
                created_at=datetime.fromisoformat(meta["created_at"]),
                confirmed_at=(
                    datetime.fromisoformat(meta["confirmed_at"])
                    if meta.get("confirmed_at")
                    else None
                ),
            )
        except (KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
        """切出 frontmatter（与 memory.frontmatter 同形的极简手写解析，读自己写的格式）。"""
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is None:
            return None
        meta: dict[str, str] = {}
        for line in lines[1:end]:
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        return meta, "\n".join(lines[end + 1 :])

    def _read_file(self, path: Path) -> AttemptRecord | None:
        try:
            return self._parse(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AttemptStoreError(f"读取尝试记录失败 {path.name}: {exc}") from exc

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
