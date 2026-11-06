"""用户生词本 + 查词历史的本地持久化（单文件 JSON）。

设计要点（与任务书一致）：

- 一个文件：<root>/state.json，UTF-8，形状
  {"version":1,"items":[...],"history":[...]}；根目录见 paths.py。
- 原子写：先写同目录临时文件，再 os.replace 换上去；进程内用
  threading.Lock 把读改写串起来，多个并发请求不会互相覆盖。
- 坏文件当空：文件不存在 / JSON 损坏 / 顶层结构不对，一律按空状态读，
  下一次写入自动修复，不把服务弄崩。
- 容量：items 最多 200 条、history 最多 50 条，都是新的在前；
  word 比较一律 casefold，Commit 与 commit 是同一条。
- 幂等：add / restore 遇到已存在的词保留原记录；import 按 word 合并，
  history 取 max(count) 与 max(at)。
- 可注入时钟：构造函数收 clock（返回毫秒整数），测试不依赖真实时间。

对外返回的 snapshot 是深拷贝，调用方随便改都不会污染下一次读盘。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .paths import state_path, wordbook_root

MAX_ITEMS = 200
MAX_HISTORY = 50
MAX_WORD_LENGTH = 64
STATE_VERSION = 1

Clock = Callable[[], int]
State = dict[str, Any]
Item = dict[str, Any]
HistoryEntry = dict[str, Any]

__all__ = [
    "MAX_HISTORY",
    "MAX_ITEMS",
    "MAX_WORD_LENGTH",
    "STATE_VERSION",
    "WordbookStore",
    "validate_word",
]


def _now_ms() -> int:
    """默认时钟：当前 Unix 毫秒时间戳（整数）。"""
    return int(time.time() * 1000)


def _is_int(value: object) -> bool:
    """排除 bool（它是 int 的子类），只认真正的整数时间戳 / 计数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def validate_word(word: object) -> str:
    """词形校验：strip 后 1..64 个字符，不能含换行；非法抛 ValueError。"""
    if not isinstance(word, str):
        raise ValueError("单词必须是字符串")
    cleaned = word.strip()
    if not cleaned:
        raise ValueError("单词不能为空")
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError("单词不能包含换行")
    if len(cleaned) > MAX_WORD_LENGTH:
        raise ValueError(f"单词长度不能超过 {MAX_WORD_LENGTH} 个字符")
    return cleaned


def _gloss_from_entry(entry: Mapping[str, Any]) -> str:
    """取 entry.senses[0].translation 做收藏快照；没有就空字符串。"""
    senses = entry.get("senses")
    if isinstance(senses, list) and senses:
        first = senses[0]
        if isinstance(first, Mapping):
            translation = first.get("translation")
            if isinstance(translation, str):
                return translation
    return ""


def _phonetic_from(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """phonetic 原样深拷贝；不是对象就当没有（前端 WordbookItem 里它是可选的）。"""
    phonetic = raw.get("phonetic")
    if isinstance(phonetic, Mapping):
        return copy.deepcopy(dict(phonetic))
    return None


def _gloss_from_item(raw: Mapping[str, Any]) -> str:
    gloss = raw.get("gloss")
    return gloss if isinstance(gloss, str) else ""


def _normalize_item(raw: object) -> Item | None:
    """把磁盘上的条目收成规范形状；word 非法就丢弃（坏数据不崩服务）。"""
    if not isinstance(raw, Mapping):
        return None
    try:
        word = validate_word(raw.get("word"))
    except ValueError:
        return None
    item: Item = {
        "word": word,
        "addedAt": raw.get("addedAt") if _is_int(raw.get("addedAt")) else 0,
        "gloss": _gloss_from_item(raw),
    }
    phonetic = _phonetic_from(raw)
    if phonetic is not None:
        item["phonetic"] = phonetic
    return item


def _normalize_history(raw: object) -> HistoryEntry | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        word = validate_word(raw.get("word"))
    except ValueError:
        return None
    at = raw.get("at")
    count = raw.get("count")
    return {
        "word": word,
        "at": at if _is_int(at) else 0,
        "count": count if _is_int(count) and count >= 1 else 1,
    }


def _dedupe_items(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    out: list[Item] = []
    for item in items:
        key = item["word"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _dedupe_history(history: list[HistoryEntry]) -> list[HistoryEntry]:
    seen: set[str] = set()
    out: list[HistoryEntry] = []
    for entry in history:
        key = entry["word"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


class WordbookStore:
    """生词本 / 查词历史的本地单文件 store（线程安全、原子写、坏文件自愈）。"""

    def __init__(
        self,
        root: str | Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._root = Path(root).expanduser() if root is not None else wordbook_root()
        self._path = state_path(self._root)
        self._clock = clock if clock is not None else _now_ms
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """状态文件路径（诊断 / 测试用）。"""
        return self._path

    # ---------- 读 / 写 ----------

    def _now(self) -> int:
        return int(self._clock())

    def _empty(self) -> State:
        return {"version": STATE_VERSION, "items": [], "history": []}

    def _load(self) -> State:
        """读状态；文件不存在 / 坏 JSON / 结构不对一律当空，绝不抛异常。"""
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return self._empty()
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return self._empty()
        if not isinstance(raw, Mapping):
            return self._empty()

        raw_items = raw.get("items")
        raw_history = raw.get("history")
        items = [
            item
            for item in (
                _normalize_item(entry)
                for entry in (raw_items if isinstance(raw_items, list) else [])
            )
            if item is not None
        ]
        history = [
            entry
            for entry in (
                _normalize_history(item)
                for item in (raw_history if isinstance(raw_history, list) else [])
            )
            if entry is not None
        ]
        items = _dedupe_items(items)
        history = _dedupe_history(history)
        items.sort(key=lambda item: item["addedAt"], reverse=True)
        history.sort(key=lambda entry: entry["at"], reverse=True)
        return {
            "version": STATE_VERSION,
            "items": items[:MAX_ITEMS],
            "history": history[:MAX_HISTORY],
        }

    def _write(self, state: State) -> None:
        """原子写：临时文件 + os.replace；失败时清掉临时文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "items": state["items"],
            "history": state["history"],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    @staticmethod
    def _snapshot(state: State) -> dict[str, list[Item] | list[HistoryEntry]]:
        return copy.deepcopy({"items": state["items"], "history": state["history"]})

    # ---------- 查询 ----------

    def snapshot(self) -> dict[str, list[Item] | list[HistoryEntry]]:
        """当前 items / history 的深拷贝。"""
        with self._lock:
            return self._snapshot(self._load())

    # ---------- 生词本 ----------

    def add(
        self,
        entry: Mapping[str, Any],
        added_at: int | None = None,
    ) -> tuple[dict[str, list[Item] | list[HistoryEntry]], Item]:
        """收藏一个词典条目；已存在（casefold 比较）保留原记录并返回它。"""
        with self._lock:
            state = self._load()
            if not isinstance(entry, Mapping):
                raise ValueError("词条格式不对")
            word = validate_word(entry.get("word"))
            key = word.casefold()
            for existing in state["items"]:
                if existing["word"].casefold() == key:
                    return self._snapshot(state), copy.deepcopy(existing)

            added = added_at if _is_int(added_at) else self._now()
            item: Item = {"word": word, "addedAt": added}
            phonetic = _phonetic_from(entry)
            if phonetic is not None:
                item["phonetic"] = phonetic
            item["gloss"] = _gloss_from_entry(entry)
            state["items"] = [item, *state["items"]][:MAX_ITEMS]
            self._write(state)
            return self._snapshot(state), copy.deepcopy(item)

    def remove(self, word: str) -> tuple[dict[str, list[Item] | list[HistoryEntry]], Item | None]:
        """移出收藏；不存在返回 removed=None（不报错）。"""
        with self._lock:
            state = self._load()
            key = validate_word(word).casefold()
            removed = next(
                (item for item in state["items"] if item["word"].casefold() == key),
                None,
            )
            if removed is None:
                return self._snapshot(state), None
            state["items"] = [item for item in state["items"] if item["word"].casefold() != key]
            self._write(state)
            return self._snapshot(state), copy.deepcopy(removed)

    def restore(
        self, item: Mapping[str, Any]
    ) -> tuple[dict[str, list[Item] | list[HistoryEntry]], Item]:
        """撤销移出：按原 addedAt 插回原位；已存在则保留原记录。"""
        with self._lock:
            state = self._load()
            if not isinstance(item, Mapping):
                raise ValueError("生词本条目格式不对")
            word = validate_word(item.get("word"))
            key = word.casefold()
            for existing in state["items"]:
                if existing["word"].casefold() == key:
                    return self._snapshot(state), copy.deepcopy(existing)

            added = item.get("addedAt")
            normalized: Item = {
                "word": word,
                "addedAt": added if _is_int(added) else self._now(),
            }
            phonetic = _phonetic_from(item)
            if phonetic is not None:
                normalized["phonetic"] = phonetic
            normalized["gloss"] = _gloss_from_item(item)
            merged = [normalized, *state["items"]]
            merged.sort(key=lambda entry: entry["addedAt"], reverse=True)
            state["items"] = merged[:MAX_ITEMS]
            self._write(state)
            return self._snapshot(state), copy.deepcopy(normalized)

    def clear_items(self) -> dict[str, list[Item] | list[HistoryEntry]]:
        """清空生词本，查词历史保留。"""
        with self._lock:
            state = self._load()
            state["items"] = []
            self._write(state)
            return self._snapshot(state)

    # ---------- 查词历史 ----------

    def record_lookup(
        self, word: str, at: int | None = None
    ) -> tuple[dict[str, list[Item] | list[HistoryEntry]], HistoryEntry]:
        """记一次查词：已有 count+1、更新时间并移到最前；最多 50 条。"""
        with self._lock:
            state = self._load()
            cleaned = validate_word(word)
            key = cleaned.casefold()
            when = at if _is_int(at) else self._now()
            history: list[HistoryEntry] = state["history"]
            for index, entry in enumerate(history):
                if entry["word"].casefold() != key:
                    continue
                updated: HistoryEntry = {
                    "word": entry["word"],
                    "at": when,
                    "count": entry["count"] + 1,
                }
                state["history"] = [
                    updated,
                    *history[:index],
                    *history[index + 1 :],
                ][:MAX_HISTORY]
                self._write(state)
                return self._snapshot(state), copy.deepcopy(updated)

            created: HistoryEntry = {"word": cleaned, "at": when, "count": 1}
            state["history"] = [created, *history][:MAX_HISTORY]
            self._write(state)
            return self._snapshot(state), copy.deepcopy(created)

    def clear_history(self) -> dict[str, list[Item] | list[HistoryEntry]]:
        """清空查词历史，生词本保留。"""
        with self._lock:
            state = self._load()
            state["history"] = []
            self._write(state)
            return self._snapshot(state)

    # ---------- 导入旧前端 localStorage ----------

    def import_state(
        self,
        items: list[Mapping[str, Any]],
        history: list[Mapping[str, Any]],
    ) -> tuple[dict[str, list[Item] | list[HistoryEntry]], int, int]:
        """按 word 合并导入；返回 (snapshot, 新增条目数, 新增历史数)。

        items：已存在的词保留原记录，只补新词；history：同词取
        max(count) 与 max(at)。先把全部输入校验完再落盘——中途发现
        非法词不会留下写了一半的状态。重复导入同一份数据返回 (snap, 0, 0)。
        """
        with self._lock:
            state = self._load()
            if not isinstance(items, list):
                raise ValueError("items 必须是数组")
            if not isinstance(history, list):
                raise ValueError("history 必须是数组")

            pending_items: list[Item] = []
            item_keys = {item["word"].casefold() for item in state["items"]}
            imported_items = 0
            for raw in items:
                if not isinstance(raw, Mapping):
                    raise ValueError("生词本条目格式不对")
                word = validate_word(raw.get("word"))
                key = word.casefold()
                if key in item_keys:
                    continue
                added = raw.get("addedAt")
                item: Item = {
                    "word": word,
                    "addedAt": added if _is_int(added) else self._now(),
                }
                phonetic = _phonetic_from(raw)
                if phonetic is not None:
                    item["phonetic"] = phonetic
                item["gloss"] = _gloss_from_item(raw)
                pending_items.append(item)
                item_keys.add(key)
                imported_items += 1

            imported_history = 0
            by_word: dict[str, HistoryEntry] = {
                entry["word"].casefold(): entry for entry in state["history"]
            }
            for raw in history:
                if not isinstance(raw, Mapping):
                    raise ValueError("查词历史条目格式不对")
                word = validate_word(raw.get("word"))
                key = word.casefold()
                at = raw.get("at")
                at = at if _is_int(at) else self._now()
                count = raw.get("count")
                count = count if _is_int(count) and count >= 1 else 1
                existing = by_word.get(key)
                if existing is not None:
                    existing["count"] = max(existing["count"], count)
                    existing["at"] = max(existing["at"], at)
                    continue
                created: HistoryEntry = {"word": word, "at": at, "count": count}
                by_word[key] = created
                state["history"].append(created)
                imported_history += 1

            merged_items = [*state["items"], *pending_items]
            merged_items.sort(key=lambda item: item["addedAt"], reverse=True)
            state["items"] = merged_items[:MAX_ITEMS]
            state["history"] = sorted(
                state["history"], key=lambda entry: entry["at"], reverse=True
            )[:MAX_HISTORY]
            self._write(state)
            return self._snapshot(state), imported_items, imported_history
