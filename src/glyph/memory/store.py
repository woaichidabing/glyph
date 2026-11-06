"""MemoryStore：记忆的唯一读写入口。

- list / get：扫描根下平铺的 ``<type>-<key>.md``，frontmatter 是唯一真相源，
  坏文件跳过（不炸扫描）；.trash 是子目录，glob('*.md') 天然碰不到；
- upsert：按 key 命中已有文件 → count+1、刷新三个日期；没命中 → 新建。
  入参条目的 count / 日期由 store 说了算，调用方只给内容；
- soft_delete：移到 .trash 并追加时间戳，绝不硬删；
- 写盘一律临时文件 + os.replace 原子替换、显式 UTF-8。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
import shutil
from pathlib import Path

from ..session.paths import local_now
from .body import build_time_hint, parse_body_sections, render_body
from .errors import MemoryEntryNotFoundError, MemoryStoreError
from .frontmatter import render_frontmatter, split_frontmatter
from .mastery import MasteryPoint, apply_point_to_entry, point_from_entry
from .paths import entry_path, review_log_path, trash_dir
from .types import MEMORY_VERSION, MemoryEntry, MemoryType

__all__ = ["MemoryStore", "parse_entry_filename"]

_FILENAME = re.compile(r"^(?P<type>mistake|preference|feedback|fact)-(?P<key>[0-9a-f]{12})\.md$")

_LOGGER = logging.getLogger(__name__)

#: 掌握判定用的字面量。与 select.MASTERED_STAGE 同一口径——
#: store 不 import select（那是渲染侧），避免多一层依赖方向。
_MASTERED_STAGE = "mastered"


def _is_mastered_mistake(entry: MemoryEntry) -> bool:
    """这条错题是否已掌握（D31：掌握之后不再排复测，目录里单列一块）。"""
    return entry.type is MemoryType.MISTAKE and entry.mastery_stage == _MASTERED_STAGE


def _optional_int(fields: dict[str, str], key: str) -> int | None:
    """读可选整数字段；缺失 / 空 / 手改坏 → None（增强字段坏不拖垮整条记忆）。"""
    raw = fields.get(key)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_entry_filename(name: str) -> tuple[MemoryType, str] | None:
    matched = _FILENAME.match(name)
    if matched is None:
        return None
    return MemoryType(matched["type"]), matched["key"]


class MemoryStore:
    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] = local_now,
    ) -> None:
        self._root = root
        self.clock = clock
        self._skipped_files = 0
        self._skipped_reviews = 0

    # ---------- 读 ----------

    @property
    def skipped_files(self) -> int:
        """最近一次扫描跳过的坏文件数（对照 Claude Code 的 parse_failed 计数）。

        单机桌面端没人盯 stderr：坏文件除了记日志，还要能从对象上读出来，
        否则"我明明记过，它怎么不见了"这种问题没法排查。
        """
        return self._skipped_files

    @property
    def skipped_reviews(self) -> int:
        """最近一次读复测流水跳过的坏行数（与 ``skipped_files`` 同一自查哲学）。"""
        return self._skipped_reviews

    def list_entries(self) -> list[MemoryEntry]:
        """扫描全部记忆文件。**坏文件跳过、其余照常**，绝不整体崩。"""
        self._skipped_files = 0
        if not self._root.is_dir():
            return []
        entries: list[MemoryEntry] = []
        for path in sorted(self._root.glob("*.md")):
            parsed = self._read_path(path)
            if parsed is not None:
                entries.append(parsed)
        return entries

    def get(self, key: str) -> MemoryEntry:
        for entry in self.list_entries():
            if entry.key == key:
                return entry
        raise MemoryEntryNotFoundError(f"记忆不存在：{key}")

    def find_mastery_point_by_code(self, code: str) -> MasteryPoint | None:
        """找到某知识点当前**背着账本的代表条目**（已建档），没有则返回 None（D15）。

        只认带掌握度阶段的 mistake（``point_from_entry`` 非 None）；同 code 的未建档
        证据句不算代表。一个 code 至多一本活账，返回文件序扫描到的第一本。
        """
        for entry in self.list_entries():
            point = point_from_entry(entry)
            if point is not None and point.error_code == code:
                return point
        return None

    def _read_path(self, path: Path) -> MemoryEntry | None:
        """读一个记忆文件；读不出来 / 结构坏 → 返回 None（跳过，不炸扫描）。

        对照 Codex（坏行 continue、文件静默不进列表）与 Claude Code（跳过 + 计数）：
        记忆是"证据"不是"契约"，坏一条丢一条，一个坏文件不该让整轮对话用不了。
        """
        if parse_entry_filename(path.name) is None:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # 不是 UTF-8（手改存成 GBK）/ 权限 / 被别的进程锁住：跳过并计数
            self._skipped_files += 1
            _LOGGER.warning("跳过读不了的记忆文件 %s: %s", path.name, exc)
            return None
        fields, body_text = split_frontmatter(text)
        try:
            type_ = MemoryType(fields["type"])
            key = fields["key"]
            entry = MemoryEntry(
                key=key,
                type=type_,
                title=fields.get("title", ""),
                quote="",
                count=int(fields["count"]),
                first_seen=fields["first_seen"],
                last_seen=fields["last_seen"],
                last_mentioned_at=fields["last_mentioned_at"],
                version=int(fields.get("version", MEMORY_VERSION)),
            )
        except (KeyError, ValueError) as exc:
            # 必填字段缺失 / 枚举非法 / count 不是数：坏文件，跳过并计数。
            # 这里**必须**计数：skipped_files 的存在理由就是让"我明明记过，它怎么不见了"
            # 能自查；只统计解码失败、漏掉字段坏，这个抓手等于半废（手改坏 frontmatter
            # 比存成 GBK 常见得多）。日志同理 —— 单机桌面端没人盯 stderr，
            # 但排查时至少有个落点。
            self._skipped_files += 1
            _LOGGER.warning("跳过字段坏掉的记忆文件 %s: %s", path.name, exc)
            return None

        sections = parse_body_sections(body_text)
        entry.quote = sections.get("用户原话", "")
        if type_ is MemoryType.MISTAKE:
            entry.tier = fields.get("tier")
            entry.issue_type = fields.get("issue_type")
            entry.error_code = fields.get("error_code")
            entry.mastery_stage = fields.get("mastery_stage")
            entry.independent_streak = _optional_int(fields, "independent_streak")
            entry.due_date = fields.get("due_date") or None
            entry.good_count = _optional_int(fields, "good_count")
            entry.hard_count = _optional_int(fields, "hard_count")
            entry.wrong_count = _optional_int(fields, "wrong_count")
            entry.last_review_at = fields.get("last_review_at") or None
            entry.reopened_count = _optional_int(fields, "reopened_count")
            # D31：怎么出的师（streak / user_claim）；旧文件没有这个字段 → None
            entry.mastered_by = fields.get("mastered_by") or None
            entry.original = sections.get("原文片段")
            entry.correction = sections.get("建议改法")
            entry.explanation = sections.get("讲解") or None
        elif type_ is MemoryType.FEEDBACK:
            entry.why = sections.get("Why")
            entry.how_to_apply = sections.get("How to apply")
        elif type_ is MemoryType.FACT:
            entry.time = build_time_hint(fields, sections)
        return entry

    # ---------- 写 ----------

    def upsert(self, entry: MemoryEntry) -> str:
        """新建或累加。返回 'created' / 'updated'。"""
        today = self.clock().date().isoformat()
        existing: MemoryEntry | None = None
        for candidate in self.list_entries():
            if candidate.key == entry.key:
                existing = candidate
                break

        if existing is None:
            entry.count = 1
            entry.first_seen = today
            entry.last_seen = today
            entry.last_mentioned_at = today
            entry.version = MEMORY_VERSION
            action = "created"
        else:
            # 身份字段沿用已存的，只累加次数与日期
            entry.count = existing.count + 1
            entry.first_seen = existing.first_seen
            entry.last_seen = today
            entry.last_mentioned_at = today
            entry.version = existing.version
            # D20：账本身份 error_code 一经建档即固定，后续任何写入（哪怕本次也带着账本
            # 阶段、哪怕想换成别的 code）都不能覆盖它；未建档错题仍可在首次建档时补 code
            if existing.mastery_stage is not None:
                entry.error_code = existing.error_code
            # 掌握度是硬状态：通用"记住"再次命中同一条、却没带账本状态时，原样保留
            # 已建档进度，绝不能被一次普通 upsert 静默清空（清账只能走专门掌握度操作）
            if existing.mastery_stage is not None and entry.mastery_stage is None:
                entry.mastery_stage = existing.mastery_stage
                entry.independent_streak = existing.independent_streak
                entry.due_date = existing.due_date
                entry.good_count = existing.good_count
                entry.hard_count = existing.hard_count
                entry.wrong_count = existing.wrong_count
                entry.last_review_at = existing.last_review_at
                entry.reopened_count = existing.reopened_count
                # D31：怎么出的师也是账本状态，同样不许被普通 upsert 抹掉
                # （真机踩到：整理器合并一条已掌握的错题后，它退回了兜底说法）
                entry.mastered_by = existing.mastered_by
            # 旧 v1 错题第一次进入掌握度账本（本次带上了阶段）：结构升级到 v2
            if entry.mastery_stage is not None and existing.mastery_stage is None:
                entry.version = MEMORY_VERSION
            action = "updated"

        path = entry_path(self._root, entry.type, entry.key)
        self._atomic_write(path, self._render_file(entry))
        self._rewrite_human_catalog()
        return action

    def soft_delete(self, key: str) -> Path:
        entry = self.get(key)  # 找不到直接抛 MemoryEntryNotFoundError
        # 若删的是背着账本的代表条目，先找同知识点、未背账的最早证据句准备过户（D20）
        point = point_from_entry(entry)
        heir: MemoryEntry | None = None
        if point is not None:
            candidates = [
                cand
                for cand in self.list_entries()
                if cand.key != key
                and cand.type is MemoryType.MISTAKE
                and cand.error_code == point.error_code
                and point_from_entry(cand) is None  # 只过户给证据句，不碰另一本账
            ]
            if candidates:
                heir = min(candidates, key=lambda c: (c.first_seen, c.key))

        # 过户顺序是安全关键：必须"先把账本写进新代表、再移走旧代表"，绝不能反过来。
        # 若先 os.replace 删旧代表、再写新代表，写盘这步一旦失败（磁盘满 / 文件被占用），
        # 旧代表已进回收站、账本就静默丢了。先写后删时：写失败则旧代表还原封不动、可重试；
        # _atomic_write 自身原子（要么写成、要么新代表原样不动），不会留下半截账。
        # （极小概率：写新代表成功后、移走旧代表前进程崩溃，会短暂"两本账"，但数据不丢，
        # 下次再删旧代表时候选过滤会自动跳过已背账的新代表、把旧代表清走，最终仍唯一。）
        if point is not None and heir is not None:
            # 账本进度（code + 8 个掌握度标量）整体迁到新代表；heir 自己的正文 / count /
            # 日期保留。直接原子写、不走 upsert（过户不累加 count、不刷日期）
            moved = replace(point, key=heir.key)
            heir_updated = apply_point_to_entry(moved, heir)
            self._atomic_write(
                entry_path(self._root, heir.type, heir.key),
                self._render_file(heir_updated),
            )

        source = entry_path(self._root, entry.type, key)
        stamp = self.clock().strftime("%Y%m%d-%H%M%S")
        target_dir = trash_dir(self._root)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{entry.type.value}-{key}-{stamp}.md"
        # 同一秒连删可能撞名：加序号，绝不覆盖回收站里的东西
        suffix = 1
        while target.exists():
            target = target_dir / f"{entry.type.value}-{key}-{stamp}-{suffix}.md"
            suffix += 1
        os.replace(source, target)
        self._rewrite_human_catalog()
        return target

    # ---------- 掌握度专用落盘（第二棒第④片）----------

    def clear_all(self) -> int:
        """清空全部记忆（软删，进 .trash 可找回）。返回移走的条数。

        连 logs/ 暂存也一起移走：整理器会从暂存里提炼记忆，
        只删条目的话，下一轮整理可能又"长"回来。
        """
        removed = 0
        for entry in self.list_entries():
            try:
                self.soft_delete(entry.key)
                removed += 1
            except Exception:  # noqa: BLE001 - 单条失败不挡其余
                continue
        logs = self._root / "logs"
        if logs.is_dir():
            target = trash_dir(self._root) / ("logs-" + self.clock().strftime("%Y%m%d-%H%M%S"))
            try:
                shutil.move(str(logs), str(target))
            except OSError:
                pass
        self._rewrite_human_catalog()
        return removed

    # ---------- 回收站 ----------

    def trash_stats(self) -> tuple[int, int]:
        """回收站里有多少个文件、共占多少字节（含 clear_all 搬进来的 logs 目录）。"""
        root = trash_dir(self._root)
        if not root.is_dir():
            return 0, 0
        count = 0
        size = 0
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    count += 1
                    size += path.stat().st_size
            except OSError:
                continue
        return count, size

    def purge_trash(self, older_than_days: int | None = None) -> tuple[int, int]:
        """永久删除回收站文件。返回 (删掉的文件数, 释放的字节数)。

        older_than_days=None 表示全删；给了天数就只删修改时间早于该天数的。
        目录清空后顺手删掉空目录；回收站不存在时什么也不做。
        """
        root = trash_dir(self._root)
        if not root.is_dir():
            return 0, 0
        cutoff = None
        if older_than_days is not None:
            cutoff = self.clock().timestamp() - older_than_days * 86400
        removed = 0
        freed = 0
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if path.is_file():
                    if cutoff is not None and path.stat().st_mtime >= cutoff:
                        continue
                    size = path.stat().st_size
                    path.unlink()
                    removed += 1
                    freed += size
                elif path.is_dir():
                    try:
                        path.rmdir()  # 只删空目录，非空会抛 OSError
                    except OSError:
                        pass
            except OSError:
                continue
        return removed, freed

    def save_mastery(self, entry: MemoryEntry) -> None:
        """掌握度账本的**唯一**写入口：只推进账本，不累加 ``count``、不刷日期、不能凭空建档。

        入参必须是从 :meth:`get` 读出的**已建档 mistake 副本**（记账工具在它上面用
        ``apply_point_to_entry`` 推进账本字段）。普通"记住"、清账、首次建档都不走这里，
        从写入口上保证档位 / 到期 / 连对只能被状态机的确定性结果改变，模型无法直接写。
        """
        if entry.type is not MemoryType.MISTAKE:
            raise MemoryStoreError("只有 mistake 错题才有掌握度账本")
        if not entry.mastery_stage or not entry.error_code:
            raise MemoryStoreError(
                "save_mastery 只能落已建档（带 mastery_stage 与 error_code）的错题"
            )
        self.get(entry.key)  # 不存在即抛 MemoryEntryNotFoundError：记账不能负责建档
        path = entry_path(self._root, entry.type, entry.key)
        self._atomic_write(path, self._render_file(entry))
        self._rewrite_human_catalog()

    # ---------- 复测流水（reviews sidecar，第二棒第④片）----------

    def append_review(self, key: str, record: dict) -> Path:
        """向 ``<root>/reviews/<key>.jsonl`` 原子追加一条复测记录并返回流水路径。

        流水是只增留痕（日期 / 判定档 / 题干 / 用户答案 / 讲解），不进 frontmatter（扁平
        key:value 存不下自由文本里的换行与引号），也不重写记忆正文或 MEMORY.md；聚合的档位
        状态在 mistake 的 frontmatter，明细在 sidecar，将来直接喂给成长页 / 评估脚本。
        """
        if not isinstance(record, dict):
            raise MemoryStoreError("复测流水必须是一条 dict 记录")
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise MemoryStoreError(f"复测流水无法序列化为 JSON：{exc}") from exc

        path = review_log_path(self._root, key)
        prefix = ""
        if path.exists():
            try:
                prefix = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise MemoryStoreError(f"读不了已有复测流水 {path.name}: {exc}") from exc
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
        self._atomic_write(path, prefix + line + "\n")
        return path

    def list_reviews(self, key: str) -> list[dict]:
        """按追加顺序读回某知识点的复测流水；坏行 / 非对象跳过并计数，绝不整体崩。"""
        self._skipped_reviews = 0
        path = review_log_path(self._root, key)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            _LOGGER.warning("读不了复测流水 %s: %s", path.name, exc)
            return []
        records: list[dict] = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (ValueError, TypeError):
                self._skipped_reviews += 1
                _LOGGER.warning("跳过一条坏掉的复测流水（%s）", path.name)
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                self._skipped_reviews += 1
        return records

    # ---------- 内部 ----------

    def _render_file(self, entry: MemoryEntry) -> str:
        fields: dict[str, str] = {
            "type": entry.type.value,
            "count": str(entry.count),
            "first_seen": entry.first_seen,
            "last_seen": entry.last_seen,
            "last_mentioned_at": entry.last_mentioned_at,
            "version": str(entry.version),
            "key": entry.key,
            "title": entry.title,
        }
        if entry.type is MemoryType.MISTAKE:
            if entry.tier is not None:
                fields["tier"] = entry.tier
            if entry.issue_type is not None:
                fields["issue_type"] = entry.issue_type
            # error_code 是知识点身份：已建档代表背账本，未建档的同码证据句也带它
            # （D15/D20：删代表时要靠它找到能过户的证据）；无码普通错题保持干净。
            if entry.error_code:
                fields["error_code"] = entry.error_code
            if entry.mastery_stage is not None:
                # 其余 8 个掌握度标量，只有背账本（已建档）的错题才写
                fields["mastery_stage"] = entry.mastery_stage
                fields["independent_streak"] = str(entry.independent_streak or 0)
                fields["good_count"] = str(entry.good_count or 0)
                fields["hard_count"] = str(entry.hard_count or 0)
                fields["wrong_count"] = str(entry.wrong_count or 0)
                fields["reopened_count"] = str(entry.reopened_count or 0)
                if entry.mastered_by:
                    fields["mastered_by"] = entry.mastered_by
                if entry.due_date is not None:
                    fields["due_date"] = entry.due_date
                if entry.last_review_at is not None:
                    fields["last_review_at"] = entry.last_review_at
        elif entry.type is MemoryType.FACT and entry.time is not None:
            time = entry.time
            fields["time_quote"] = time.quote
            fields["time_approx"] = "true" if time.approximate else "false"
            if time.exact is not None:
                fields["time_exact"] = time.exact
            else:
                fields["time_span_start"] = time.span_start or ""
                fields["time_span_end"] = time.span_end or ""
        return render_frontmatter(fields) + render_body(entry)

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise MemoryStoreError(f"写不了记忆文件 {path.name}: {exc}") from exc

    #: 掌握依据的展示写法（D31）。模型看到的目录只列在跟的；
    #: 这个给人看的文件把已掌握的单列一块，好让用户看清"练掉过什么"。
    _MASTERED_BASIS_ZH = {
        "streak": "复测连对达标",
        "user_claim": "自述 + 出一道题验证通过",
    }

    def _rewrite_human_catalog(self) -> None:
        """重写给人看的 MEMORY.md（工具从不读它，只作逃生门）。

        分两块（D31）：
        - **在跟的**：会影响以后复习的（未掌握 / 非错题）；
        - **已掌握**：不再安排复测的错题，带日期与掌握依据。

        已掌握的**不删**——用户能看到"学过什么、什么时候练掉的"；
        文件也还在（以后又犯会自动复活，靠 reopened_count 认出来）。
        """
        entries = sorted(
            self.list_entries(),
            key=lambda e: (e.last_mentioned_at, e.count),
            reverse=True,
        )
        active = [e for e in entries if not _is_mastered_mistake(e)]
        mastered = [e for e in entries if _is_mastered_mistake(e)]

        lines = ["# 记忆目录（给人看；工具每轮从各文件重新扫描，不读本文件）", ""]
        lines += [f"- [{e.type.value}] {e.title}" for e in active]

        if mastered:
            lines += [
                "",
                "## 已掌握（不再安排复测；以后作文里又犯同一错会自动复活）",
                "",
            ]
            for entry in mastered:
                basis = self._MASTERED_BASIS_ZH.get(
                    entry.mastered_by or "", "复测连对达标"
                )
                lines.append(
                    f"- [{entry.type.value}] {entry.title}"
                    f" — {entry.last_mentioned_at} {basis}"
                )

        path = self._root / "MEMORY.md"
        self._atomic_write(path, "\n".join(lines) + "\n")
