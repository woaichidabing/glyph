"""整理器（D29）：把暂存材料合并进正式记忆。

低频后台任务，四阶段照 CC 的 Dream：看清现状 → 收集信号 → 合并 → 修剪。
三个安全机制（也照 CC）：
- 锁：别的进程正在整理就不跑，防互相覆盖；
- 失败回滚 + 推迟下次：不立刻重试，免得反复烧钱；
- 读不出来就跳过整次，并说清原因与恢复条件——不拿脏数据乱改。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .extract import ExtractLLM
from .pending import list_pending, mark_done
from .store import MemoryStore
from .types import MemoryEntry, MemoryType

__all__ = ["ConsolidateOutcome", "MemoryConsolidator", "LOCK_NAME"]

LOCK_NAME = ".consolidate.lock"
_STATE_NAME = ".consolidate.state.json"


class ConsolidateOutcome(StrEnum):
    """一次整理的结论。跳过原因分开枚举，日志里能看出为什么没跑。"""

    DONE = "done"
    FAILED = "failed"
    SKIP_TOO_SOON = "skip_too_soon"
    SKIP_TOO_FEW_SESSIONS = "skip_too_few_sessions"
    SKIP_NOTHING_PENDING = "skip_nothing_pending"
    SKIP_THROTTLED = "skip_throttled"
    SKIP_LOCKED = "skip_locked"
    SKIP_UNREADABLE = "skip_unreadable"


_SYSTEM = """你在做一次记忆整理。把暂存区的材料合并进长期记忆，让以后的会话能快速进入状态。

## 第一阶段 · 看清现状
下面给了现在的记忆清单；能改进已有条目就不要新建近似重复。

## 第二阶段 · 收集信号
只看给你的暂存材料和现有记忆。不要额外去查证别的。

## 第三阶段 · 合并
- 合并进已有主题，而不是建近似重复；
- 把相对日期（"昨天""上周"）转成具体日期，否则以后看不懂；
- 删掉被推翻的：如果新证据推翻了旧记忆，就在源头改掉。

## 第四阶段 · 修剪
- 两个文件矛盾时，修错的那个；分不清算不算冲突，就两条都留着；
- 错题学会了的（用户说会了、或复测连对掌握）：标 mastered，退出目录，文件保留；
- 偏好反转（之前要中文、现在要英文）：新的覆盖旧的，但留一行旧的是什么、什么时候改的。

## 输出格式
只输出一段 JSON，不要别的话：
{{"writes": [{{"key": "12位十六进制；新建时自己给一个稳定的",
  "type": "mistake|preference|feedback|fact", "title": "一句话标题",
  "quote": "用户原话", "retire": false, "note": "可选：改了什么"}}]}}
没有要改的就输出 {{"writes": []}}
"""

_USER_TEMPLATE = """## 现有记忆（{existing_count} 条）
{existing}

## 暂存材料（{pending_count} 份）
{pending}
"""

_VALID_TYPES = {t.value for t in MemoryType}

def _parse_writes(text: str) -> list[dict[str, Any]] | None:
    """解析整理结果；格式不对返回 None（调用方据此判定失败）。"""
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    body = body.strip()
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("writes")
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        title = str(item.get("title") or "").strip()
        if not key or not title:
            continue
        kind = str(item.get("type") or "").strip()
        if kind not in _VALID_TYPES:
            kind = "fact"
        out.append(
            {
                "key": key,
                "type": kind,
                "title": title,
                "quote": str(item.get("quote") or "").strip(),
                "retire": bool(item.get("retire")),
            }
        )
    return out


@dataclass(frozen=True)
class ConsolidateState:
    last_consolidated_at: str = ""
    last_scan_at: str = ""
    unreadable_reason: str = ""


class MemoryConsolidator:
    """一次整理：读暂存 + 现有记忆 → 问模型 → 写正式记忆。"""

    def __init__(
        self,
        *,
        root: Path,
        store: MemoryStore,
        llm: ExtractLLM,
        model: str,
        min_hours: int = 24,
        min_sessions: int = 3,
        throttle_seconds: int = 600,
    ) -> None:
        self._root = root
        self._store = store
        self._llm = llm
        self._model = model
        self._min_hours = max(0, min_hours)
        self._min_sessions = max(1, min_sessions)
        self._throttle = max(0, throttle_seconds)
        self._state = self._load_state()
        self._notice = ""

    # ---- 只读 ----

    def last_notice(self) -> str:
        """上次跳过的可见说明（原因 + 何时恢复）。没人看也不能默默失败。"""
        return self._notice

    # ---- 状态（测试与接线用）----

    def mark_consolidated(self, *, at: datetime) -> None:
        self._state = ConsolidateState(
            last_consolidated_at=at.isoformat(),
            last_scan_at=self._state.last_scan_at,
            unreadable_reason="",
        )
        self._save_state()

    def mark_scanned(self, *, at: datetime) -> None:
        self._state = ConsolidateState(
            last_consolidated_at=self._state.last_consolidated_at,
            last_scan_at=at.isoformat(),
            unreadable_reason=self._state.unreadable_reason,
        )
        self._save_state()

    def mark_memory_unreadable(self, *, reason: str) -> None:
        self._state = ConsolidateState(
            last_consolidated_at=self._state.last_consolidated_at,
            last_scan_at=self._state.last_scan_at,
            unreadable_reason=reason,
        )
        self._save_state()

    # ---- 主流程 ----

    async def run(self, *, now: datetime, sessions_touched: int) -> ConsolidateOutcome:
        """跑一次整理。sessions_touched 是自上次整理以来动过的会话数。"""
        self._notice = ""

        if self._state.unreadable_reason:
            reason = self._state.unreadable_reason
            self._notice = (
                f"这次整理跳过了：记忆读不出来（{reason}）。"
                "修好读写权限后，下次到点会自动继续，不用手动触发。"
            )
            return ConsolidateOutcome.SKIP_UNREADABLE

        if self._hours_since_last(now) < self._min_hours:
            return ConsolidateOutcome.SKIP_TOO_SOON
        if sessions_touched < self._min_sessions:
            return ConsolidateOutcome.SKIP_TOO_FEW_SESSIONS
        since_scan = self._seconds_since_scan(now)
        if since_scan is not None and since_scan < self._throttle:
            return ConsolidateOutcome.SKIP_THROTTLED

        pending = list_pending(self._root)
        if not pending:
            return ConsolidateOutcome.SKIP_NOTHING_PENDING
        if not self._acquire_lock():
            return ConsolidateOutcome.SKIP_LOCKED

        try:
            return await self._consolidate(pending, now=now)
        finally:
            self._release_lock()

    async def _consolidate(self, pending: list[Path], *, now: datetime) -> ConsolidateOutcome:
        existing = self._store.list_entries()
        pending_text = "\n".join(
            f"### {p.name}\n{p.read_text(encoding='utf-8')}" for p in pending
        )
        existing_text = (
            "\n".join(f"- [{e.type.value}] {e.key} {e.title}" for e in existing)
            or "（还没有）"
        )
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _USER_TEMPLATE.format(
                    existing_count=len(existing),
                    existing=existing_text,
                    pending_count=len(pending),
                    pending=pending_text,
                ),
            },
        ]

        try:
            reply = await self._llm.complete(messages, model=self._model)
        except Exception:
            # 失败：推迟下次触发（不立刻重试），暂存与记忆都不动
            self.mark_consolidated(at=now)
            return ConsolidateOutcome.FAILED

        parsed = _parse_writes(reply)
        if parsed is None:
            self.mark_consolidated(at=now)
            return ConsolidateOutcome.FAILED

        for item in parsed:
            self._apply(item, today=now.date())

        # 合并成功才收走暂存，保证「至少处理一次」
        for path in pending:
            mark_done(path)
        self.mark_consolidated(at=now)
        return ConsolidateOutcome.DONE

    def _apply(self, item: dict[str, Any], *, today: date) -> None:
        """合并一条：有同 key 的就更新，没有就新建。"""
        key = str(item["key"])[:64]
        try:
            existing = self._store.get(key)
        except Exception:
            existing = None  # 还没有这条，按新建处理

        kind = MemoryType(str(item["type"]))
        title = str(item["title"])
        quote = str(item.get("quote") or "") or title
        today_iso = today.isoformat()

        if existing is None:
            entry = MemoryEntry(
                key=key,
                type=kind,
                title=title,
                quote=quote,
                count=1,
                first_seen=today_iso,
                last_seen=today_iso,
                last_mentioned_at=today_iso,
            )
        else:
            # 合并：只交代「内容」（标题 / 原话 / 身份），账本字段一律不复制过来。
            #
            # 交给 upsert 去保护是对的，而且必须这样：count 与 last_* 由 upsert 自己
            # 按"提了一次"累加（这里再算一遍会加成两次）；掌握度那一整套也由它按
            # existing 原样保留（这里复制一遍，一旦漏掉某个字段就会静默丢状态——
            # 真机就是这样把 mastered_by 丢掉的）。
            entry = MemoryEntry(
                key=existing.key,
                type=kind,
                title=title,
                quote=quote,
                count=1,
                first_seen=today_iso,
                last_seen=today_iso,
                last_mentioned_at=today_iso,
                tier=existing.tier,
                issue_type=existing.issue_type,
                error_code=existing.error_code,
                original=existing.original or "",
                correction=existing.correction or "",
            )
        self._store.upsert(entry)

    # ---- 时间与锁 ----

    def _hours_since_last(self, now: datetime) -> float:
        stamp = self._state.last_consolidated_at
        if not stamp:
            return float("inf")  # 从没整理过 → 不拦
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return float("inf")
        return (now - last).total_seconds() / 3600

    def _seconds_since_scan(self, now: datetime) -> float | None:
        stamp = self._state.last_scan_at
        if not stamp:
            return None
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return (now - last).total_seconds()

    def _lock_path(self) -> Path:
        return self._root / LOCK_NAME

    def _acquire_lock(self) -> bool:
        path = self._lock_path()
        try:
            # O_EXCL：已存在就失败，不覆盖别人的锁
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.close(fd)
            return True
        except FileExistsError:
            return False
        except OSError:
            return False

    def _release_lock(self) -> None:
        try:
            self._lock_path().unlink()
        except FileNotFoundError:
            pass

    # ---- 状态落盘 ----

    def _state_path(self) -> Path:
        return self._root / _STATE_NAME

    def _load_state(self) -> ConsolidateState:
        path = self._state_path()
        if not path.exists():
            return ConsolidateState()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ConsolidateState()
        if not isinstance(data, dict):
            return ConsolidateState()
        return ConsolidateState(
            last_consolidated_at=str(data.get("last_consolidated_at") or ""),
            last_scan_at=str(data.get("last_scan_at") or ""),
            unreadable_reason=str(data.get("unreadable_reason") or ""),
        )

    def _save_state(self) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "last_consolidated_at": self._state.last_consolidated_at,
                "last_scan_at": self._state.last_scan_at,
                "unreadable_reason": self._state.unreadable_reason,
            },
            ensure_ascii=False,
        )
        path.write_text(payload, encoding="utf-8")
