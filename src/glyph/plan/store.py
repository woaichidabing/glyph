"""计划目标 / 任务的本地单文件仓库。

- 一个文件：<root>/state.json；UTF-8；线程锁 + 临时文件 + os.replace 原子写。
- 坏文件按空状态读，不崩服务。
- 任务最多 500 条；目标最多 200 字；任务文本最多 200 字。
- 自动完成只碰“今天”的任务：scheduledFor 为空时看 createdAt；有 scheduledFor 时只认当天。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .paths import plan_root, state_path

STATE_VERSION = 2
MAX_TASKS = 500
MAX_TEXT = 200
MAX_WEEKS = 52
KINDS = ("study", "dict", "custom")
WEEKDAYS = ("1", "2", "3", "4", "5", "6", "7")  # 周一=1 … 周日=7
MAX_RHYTHM_ITEMS = 8  # 每天最多排几件，防止模板撑爆页面

Clock = Callable[[], int]
Today = Callable[[], str]
Snapshot = dict[str, Any]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _today() -> str:
    return date.today().isoformat()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("文本必须是字符串")
    text = value.strip()
    if not text:
        raise ValueError("文本不能为空")
    if "\n" in text or "\r" in text:
        raise ValueError("文本不能包含换行")
    if len(text) > MAX_TEXT:
        raise ValueError(f"文本不能超过 {MAX_TEXT} 个字符")
    return text


def infer_kind(text: str) -> str:
    """没有显式 kind 时按任务文案推断；只做保守推断，认不出就当自定义。"""
    if any(word in text for word in ("背", "单词", "词汇", "六级词", "四级词")):
        return "study"
    if any(word in text for word in ("查词", "查过", "生词", "词典")):
        return "dict"
    return "custom"


def _normalize_kind(value: object) -> str:
    return value if value in KINDS else "custom"


def _normalize_task(raw: Mapping[str, Any], today: str) -> dict[str, Any]:
    text = _clean_text(raw.get("t") or raw.get("text"))
    kind = _normalize_kind(raw.get("kind"))
    if not raw.get("kind"):
        kind = infer_kind(text)
    target = raw.get("target", 1)
    if not _is_int(target) or target < 1:
        target = 1
    scheduled = raw.get("scheduledFor") or raw.get("scheduled_for")
    if not isinstance(scheduled, str) or len(scheduled) != 10:
        scheduled = None
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex),
        "t": text,
        "src": raw.get("src") if raw.get("src") in ("glyph", "own") else "glyph",
        "kind": kind,
        "target": target,
        "progress": int(raw.get("progress") or 0) if _is_int(raw.get("progress")) else 0,
        "done": bool(raw.get("done")),
        "createdAt": raw.get("createdAt") if isinstance(raw.get("createdAt"), str) else today,
        "scheduledFor": scheduled,
        "doneAt": raw.get("doneAt") if _is_int(raw.get("doneAt")) else None,
        "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), str) else None,
    }


def _normalize_goal(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        text = _clean_text(raw.get("text"))
    except ValueError:
        return None
    weeks = raw.get("weeks", 12)
    if not _is_int(weeks) or not 1 <= weeks <= MAX_WEEKS:
        weeks = 12
    start = raw.get("startDate") or raw.get("start_date")
    if not isinstance(start, str) or len(start) != 10:
        start = _today()
    return {
        "text": text,
        "weeks": weeks,
        "startDate": start,
        "createdAt": raw.get("createdAt") if _is_int(raw.get("createdAt")) else 0,
    }


def _normalize_phases(raw: object) -> list[dict[str, Any]]:
    """阶段：{"fromWeek","toWeek","title","focus"}；越界或反序的整条丢弃。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:12]:
        if not isinstance(item, Mapping):
            continue
        try:
            title = _clean_text(item.get("title"))
        except ValueError:
            continue
        start = item.get("fromWeek")
        end = item.get("toWeek")
        if not _is_int(start) or not _is_int(end) or start < 1 or end < start:
            continue
        focus = item.get("focus")
        out.append({
            "fromWeek": start,
            "toWeek": min(end, MAX_WEEKS),
            "title": title,
            "focus": focus.strip()[:MAX_TEXT] if isinstance(focus, str) else "",
        })
    return out


def _normalize_rhythm(raw: object) -> dict[str, list[dict[str, Any]]]:
    """周节奏：{"1": [{"t","kind"}], ...}；周一=1。空的一天就是休息。"""
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, Mapping):
        return out
    for day in WEEKDAYS:
        items = raw.get(day) or raw.get(int(day))
        if not isinstance(items, list):
            continue
        cleaned: list[dict[str, Any]] = []
        for item in items[:MAX_RHYTHM_ITEMS]:
            if isinstance(item, str):
                item = {"t": item}
            if not isinstance(item, Mapping):
                continue
            try:
                text = _clean_text(item.get("t") or item.get("text"))
            except ValueError:
                continue
            cleaned.append({"t": text, "kind": _normalize_kind(item.get("kind"))})
        if cleaned:
            out[day] = cleaned
    return out


def _weekday_of(iso: str) -> str:
    """ISO 日期 → 周几（周一=1）。"""
    return str(date.fromisoformat(iso).isoweekday())


def _week_ahead(today: str) -> list[str]:
    """今天到本周日（含）的日期；周日就到今天为止。"""
    base = date.fromisoformat(today)
    monday = base - timedelta(days=base.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    out: list[str] = []
    d = base
    while d <= sunday:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _rebuild_week_pending(state: Snapshot, today: str) -> None:
    """按当前节奏重建"今天到周日"的未完成任务；已完成的保留。"""
    days = set(_week_ahead(today))
    prefix = tuple(f"r-{d}-" for d in days)
    state["tasks"] = [
        task for task in state["tasks"]
        if not (str(task.get("id", "")).startswith(prefix) and not task.get("done"))
    ]
    rhythm = state.get("rhythm") or {}
    existing = {task.get("id") for task in state["tasks"]}
    for day in days:
        for index, item in enumerate(rhythm.get(_weekday_of(day)) or []):
            task_id = f"r-{day}-{index}"
            suffix = 1
            while task_id in existing:
                task_id = f"r-{day}-{index}-{suffix}"
                suffix += 1
            if len(state["tasks"]) >= MAX_TASKS:
                continue
            state["tasks"].append({
                "id": task_id,
                "t": item["t"],
                "src": "glyph",
                "kind": item.get("kind") or infer_kind(item["t"]),
                "target": 1,
                "progress": 0,
                "done": False,
                "createdAt": today,
                "scheduledFor": day,
                "doneAt": None,
                "evidence": None,
            })
            existing.add(task_id)


def _materialize_today(state: Snapshot, today: str) -> bool:
    """把今天的周节奏落成真实任务；幂等（同一天同一格只生成一次）。

    为什么要落成任务、而不是每天现算：勾选状态、顺延、完成时间都得有个地方存。
    id 里带日期，所以昨天生成的不会占今天的位，昨天的没做完的靠前端"未完成仍列出"顺延。
    """
    rhythm = state.get("rhythm") or {}
    if not rhythm:
        return False
    existing = {task.get("id") for task in state["tasks"]}
    added = False
    for day in _week_ahead(today):
        items = rhythm.get(_weekday_of(day)) or []
        for index, item in enumerate(items):
            task_id = f"r-{day}-{index}"
            if task_id in existing:
                continue
            if len(state["tasks"]) >= MAX_TASKS:
                return added
            state["tasks"].append({
                "id": task_id,
                "t": item["t"],
                "src": "glyph",
                "kind": item.get("kind") or infer_kind(item["t"]),
                "target": 1,
                "progress": 0,
                "done": False,
                "createdAt": today,
                "scheduledFor": day,
                "doneAt": None,
                "evidence": None,
            })
            existing.add(task_id)
            added = True
    return added


class PlanStore:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Clock = _now_ms,
        today: Today = _today,
    ) -> None:
        base = Path(root).expanduser() if root is not None else plan_root()
        self._path = state_path(base)
        self._clock = clock
        self._today = today
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def today(self) -> str:
        return self._today()

    def _empty(self) -> Snapshot:
        return {"version": STATE_VERSION, "goal": None, "phases": [], "rhythm": {}, "tasks": []}

    def _load(self) -> Snapshot:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return self._empty()
        if not isinstance(data, Mapping):
            return self._empty()
        tasks = []
        raw_tasks = data.get("tasks")
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if not isinstance(item, Mapping):
                    continue
                try:
                    tasks.append(_normalize_task(item, self._today()))
                except ValueError:
                    continue
        return {
            "version": STATE_VERSION,
            "goal": _normalize_goal(data.get("goal")),
            "phases": _normalize_phases(data.get("phases")),
            "rhythm": _normalize_rhythm(data.get("rhythm")),
            "tasks": tasks[:MAX_TASKS],
        }

    def _save(self, state: Snapshot) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def snapshot(self) -> Snapshot:
        with self._lock:
            state = self._load()
            if _materialize_today(state, self._today()):
                self._save(state)
            return copy.deepcopy(state)

    def set_goal(self, text: str, *, weeks: int = 12, start_date: str | None = None) -> Snapshot:
        clean = _clean_text(text)
        if not _is_int(weeks) or not 1 <= weeks <= MAX_WEEKS:
            raise ValueError(f"weeks 必须是 1..{MAX_WEEKS} 的整数")
        start = start_date or self._today()
        if not isinstance(start, str) or len(start) != 10:
            raise ValueError("start_date 必须是 YYYY-MM-DD")
        with self._lock:
            state = self._load()
            state["goal"] = {
                "text": clean,
                "weeks": weeks,
                "startDate": start,
                "createdAt": self._clock(),
            }
            self._save(state)
            return copy.deepcopy(state)

    def reset(self) -> Snapshot:
        """清空整份计划（目标 / 阶段 / 节奏 / 任务），换计划时用。"""
        with self._lock:
            state = self._empty()
            self._save(state)
            return copy.deepcopy(state)

    def set_phases(self, phases: Sequence[Mapping[str, Any]]) -> Snapshot:
        """整本替换阶段表。传空列表 = 清空阶段。"""
        cleaned = _normalize_phases(list(phases))
        with self._lock:
            state = self._load()
            state["phases"] = cleaned
            self._save(state)
            return copy.deepcopy(state)

    def replace_day(
        self,
        day: str,
        items: Sequence[Mapping[str, Any] | str],
        *,
        scope: str = "day",
    ) -> Snapshot:
        """把某一天的任务换成 items。

        scope="day"：只改这一天（写真实任务，不动模板）；
        scope="weekly"：同时把这一天的条目写回周节奏，并把本周剩下的日子
        按新节奏重建（已完成的保留）。

        一律**不删已完成的**：完成了就是完成了，改计划不能把记录抹掉。
        """
        want = [dict(x) if isinstance(x, Mapping) else {"t": str(x)} for x in items]
        want = [{"t": str(x.get("t", "")).strip()} for x in want if str(x.get("t", "")).strip()]
        today = self._today()
        with self._lock:
            state = self._load()
            if scope == "weekly":
                weekday = _weekday_of(day)
                rhythm = dict(state.get("rhythm") or {})
                rhythm[weekday] = want
                state["rhythm"] = _normalize_rhythm(rhythm)
            # 删掉这一天还没完成的节奏任务
            state["tasks"] = [
                task for task in state["tasks"]
                if not (
                    str(task.get("id", "")).startswith(f"r-{day}-")
                    and not task.get("done")
                )
            ]
            existing = {task.get("id") for task in state["tasks"]}
            for index, item in enumerate(want):
                if len(state["tasks"]) >= MAX_TASKS:
                    break
                task_id = f"r-{day}-{index}"
                # 已完成的老任务会占着同名 id：换一个后缀，别把新条目吞掉
                suffix = 1
                while task_id in existing:
                    task_id = f"r-{day}-{index}-{suffix}"
                    suffix += 1
                state["tasks"].append({
                    "id": task_id,
                    "t": item["t"],
                    "src": "glyph",
                    "kind": infer_kind(item["t"]),
                    "target": 1,
                    "progress": 0,
                    "done": False,
                    "createdAt": today,
                    "scheduledFor": day,
                    "doneAt": None,
                    "evidence": None,
                })
                existing.add(task_id)
            if scope == "weekly":
                # 本周剩下的日子也按新节奏重建（今天已按上面处理过，重复跑是幂等的）
                _rebuild_week_pending(state, today)
            self._save(state)
            return copy.deepcopy(state)

    def set_rhythm(self, rhythm: Mapping[str, Any], *, sync: bool = False) -> Snapshot:
        """整本替换周节奏。传空 = 不按节奏排（只留手动任务）。

        sync=True：本周剩下的日子按新节奏重建（未完成的删掉重生成，已完成的保留）。
        """
        cleaned = _normalize_rhythm(dict(rhythm))
        with self._lock:
            state = self._load()
            state["rhythm"] = cleaned
            if sync:
                _rebuild_week_pending(state, self._today())
            else:
                _materialize_today(state, self._today())
            self._save(state)
            return copy.deepcopy(state)

    def add_tasks(
        self,
        tasks: Sequence[Mapping[str, Any] | str],
        *,
        src: str = "glyph",
        scheduled_for: str | None = None,
    ) -> Snapshot:
        today = self._today()
        normalized: list[dict[str, Any]] = []
        for raw in tasks:
            if isinstance(raw, str):
                raw = {"t": raw}
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            item.setdefault("src", src)
            item.setdefault("createdAt", today)
            if scheduled_for and not item.get("scheduledFor"):
                item["scheduledFor"] = scheduled_for
            try:
                normalized.append(_normalize_task(item, today))
            except ValueError:
                continue
        if not normalized:
            raise ValueError("没有有效任务")
        with self._lock:
            state = self._load()
            existing = state["tasks"]
            rooms = MAX_TASKS - len(existing)
            if rooms <= 0:
                raise ValueError(f"任务最多 {MAX_TASKS} 条")
            state["tasks"] = (existing + normalized)[:MAX_TASKS]
            self._save(state)
            return copy.deepcopy(state)

    def toggle_task(self, task_id: str, *, done: bool | None = None) -> Snapshot:
        with self._lock:
            state = self._load()
            found = None
            for task in state["tasks"]:
                if task["id"] != task_id:
                    continue
                target = (not task["done"]) if done is None else bool(done)
                task["done"] = target
                task["doneAt"] = self._clock() if target else None
                if not target:
                    task["evidence"] = None
                    task["progress"] = 0
                found = copy.deepcopy(task)
                break
            if found is None:
                raise KeyError(task_id)
            self._save(state)
            return {**copy.deepcopy(state), "task": found}

    def delete_task(self, task_id: str) -> Snapshot:
        with self._lock:
            state = self._load()
            found = None
            kept = []
            for task in state["tasks"]:
                if task["id"] == task_id and found is None:
                    found = task
                else:
                    kept.append(task)
            if found is None:
                raise KeyError(task_id)
            state["tasks"] = kept
            self._save(state)
            return {**copy.deepcopy(state), "removed": copy.deepcopy(found)}

    def complete_kind(self, kind: str, *, evidence: str | None = None, amount: int = 1) -> Snapshot:
        if kind not in ("study", "dict"):
            raise ValueError("kind 必须是 study 或 dict")
        if not _is_int(amount) or amount < 1:
            amount = 1
        today = self._today()
        with self._lock:
            state = self._load()
            found = None
            for task in state["tasks"]:
                if task["done"] or task["kind"] != kind:
                    continue
                if task["scheduledFor"] not in (None, today):
                    continue
                if task["scheduledFor"] is None and task["createdAt"] > today:
                    continue
                task["progress"] = min(task["target"], task["progress"] + amount)
                if task["progress"] >= task["target"]:
                    task["done"] = True
                    task["doneAt"] = self._clock()
                if evidence:
                    task["evidence"] = evidence[:MAX_TEXT]
                found = copy.deepcopy(task)
                break
            self._save(state)
            return {**copy.deepcopy(state), "task": found}

    def import_state(
        self,
        goal: Mapping[str, Any] | None,
        tasks: Sequence[Mapping[str, Any]],
    ) -> Snapshot:
        today = self._today()
        with self._lock:
            state = self._load()
            imported = 0
            if state["goal"] is None:
                normalized_goal = _normalize_goal(goal)
                if normalized_goal is not None:
                    normalized_goal["createdAt"] = self._clock()
                    state["goal"] = normalized_goal
            ids = {task["id"] for task in state["tasks"]}
            for raw in tasks:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    task = _normalize_task(raw, today)
                except ValueError:
                    continue
                if task["id"] in ids:
                    continue
                if len(state["tasks"]) >= MAX_TASKS:
                    break
                state["tasks"].append(task)
                ids.add(task["id"])
                imported += 1
            self._save(state)
            return {**copy.deepcopy(state), "importedTasks": imported}
