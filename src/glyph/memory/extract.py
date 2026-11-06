"""提炼器（D27）：把一批对话变成暂存材料，等整理器合并。

用便宜模型（不是主脑），产物只写暂存、**不直接改正式记忆**。
提示词结构照 Claude Code 的 memory extraction subagent：
- 先给现有记忆清单，要求"能改旧的就不新建"；
- 只准用给的材料，不许去调查核实；
- 没东西可记就闭嘴（Nothing to save.）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .pending import append_pending

__all__ = ["MemoryExtractor", "ExtractResult", "ExtractLLM"]



class ExtractLLM(Protocol):
    """提炼用的最小模型接口（一次问答，不用流式、不用工具）。

    这样测试能塞假模型，也不用把 llm/ 那一整套拖进 memory/（R7）。
    """

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str: ...



@dataclass(frozen=True)
class ExtractResult:
    """一次提炼的结果。出错也要如实带出来，不许静默。"""

    saved: bool
    saved_count: int = 0
    attempts: int = 0
    path: str | None = None
    error: str = ""
    titles: list[str] = field(default_factory=list)



_ROLE_PREFIX = {"user": ">", "assistant": "<", "tool": "."}

_SYSTEM = """你是记忆提炼员。看下面最近这几轮对话，提炼出值得长期记住的东西。

## 已经记着的（写之前先看：能在旧的上面改就不要新建一条）
{existing}

## 什么该记、什么不该记
- 能现在读出来的不记：对话里已有的原话、刚发生过的一次性事情、临时状态；
- 看"反复"：同一个信号出现两次以上才算真信号；只提过一次的，先记下但别当成定论；
- 偏好和对助手的要求，记下来就是终态；错题是学习的中间状态，用户迟早会学会；
- 不确定要不要记的，宁可不记。

## 纪律
- 只准用下面给的材料，不要去查证别的（不要翻文件、不要用别的工具）；
- 用户说的原话要逐字保留，不要改写成你的总结；
- 没东西可记时，只输出 Nothing to save. 不要解释。

## 输出格式
只输出一段 JSON，不要别的话：
{{"memories": [{{"type": "mistake|preference|feedback|fact", "title": "一句话标题",
  "quote": "用户原话", "error_code": "仅错题，六类之一，可不填"}}]}}
没东西可记就输出 {{"memories": []}}
"""


_USER_TEMPLATE = """以下是最近这几轮对话（每行前面的符号：> 用户，< 助手，. 工具）：

{transcript}
"""


_VALID_CODES = {"VERB:TENSE", "MORPH", "NOUN:NUM", "NOUN:INFL", "DET", "SPELL"}
_VALID_TYPES = {"mistake", "preference", "feedback", "fact"}



def _strip_code_fence(text: str) -> str:
    """模型爱把 JSON 包在代码围栏里，剥掉。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()



def _parse_memories(text: str) -> list[dict[str, Any]] | None:
    """解析模型回复；格式不对返回 None（调用方据此重试）。

    "Nothing to save." 视为"空结果"而非坏格式——那是约定的正常出口。
    """
    body = _strip_code_fence(text)
    if not body:
        return None
    if body.lower().startswith("nothing to save"):
        return []
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("memories")
    if not isinstance(items, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue  # 没标题的条目等于没说什么，丢掉
        kind = str(item.get("type") or "").strip()
        if kind not in _VALID_TYPES:
            kind = "fact"
        code = str(item.get("error_code") or "").strip()
        entry: dict[str, Any] = {
            "type": kind,
            "title": title,
            "quote": str(item.get("quote") or "").strip(),
        }
        if code in _VALID_CODES:
            entry["error_code"] = code
        cleaned.append(entry)
    return cleaned



def _is_prose(entry: dict[str, Any]) -> bool:
    """这条材料里有没有用户打的字（工具行不算）。"""
    return entry.get("role") == "user" and bool(str(entry.get("text") or "").strip())



def _render_transcript(transcript: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in transcript:
        role = str(entry.get("role") or "")
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        prefix = _ROLE_PREFIX.get(role, "?")
        lines.append(f"{prefix} {text}")
    return "\n".join(lines)



def _render_pending_lines(parsed: list[dict[str, Any]]) -> list[str]:
    """把解析出来的条目渲染成暂存行（一行一条）。"""
    lines: list[str] = []
    for entry in parsed:
        kind = str(entry["type"])
        title = str(entry["title"])
        quote = str(entry.get("quote") or "")
        suffix = f"  ｜原话：{quote}" if quote else ""
        lines.append(f"- [{kind}] {title}{suffix}")
    return lines



class MemoryExtractor:
    """一次提炼：读材料 → 问模型 → 写暂存。

    它**不碰正式记忆**——那是整理器的事（D29）。
    """


    def __init__(
        self,
        *,
        root: Path,
        llm: ExtractLLM,
        model: str,
        max_rounds: int = 4,
    ) -> None:
        self._root = root
        self._llm = llm
        self._model = model
        self._max_rounds = max(1, max_rounds)

    async def run(
        self,
        *,
        session_id: str,
        title: str,
        transcript: list[dict[str, Any]],
        day: date,
        existing_titles: list[str] | None = None,
    ) -> ExtractResult:
        """跑一次提炼。返回结果如实说明存没存下、试了几次、错在哪。"""
        if not transcript:
            return ExtractResult(saved=False, attempts=0)
        if not any(_is_prose(e) for e in transcript):
            # D27 第一条过滤：没打过字就没东西可提炼，别花钱
            return ExtractResult(saved=False, attempts=0)

        rendered = _render_transcript(transcript)
        if not rendered.strip():
            return ExtractResult(saved=False, attempts=0)


        existing = "\n".join(f"- {t}" for t in (existing_titles or [])) or "（还没有）"
        system = _SYSTEM.format(existing=existing)
        user = _USER_TEMPLATE.format(transcript=rendered)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        last_error = ""
        attempt = 0
        for attempt in range(1, self._max_rounds + 1):
            try:
                reply = await self._llm.complete(messages, model=self._model)
            except Exception as exc:  # 网络 / 模型错：如实上报，不静默
                last_error = f"{type(exc).__name__}: {exc}"
                break
            parsed = _parse_memories(reply)
            if parsed is None:
                last_error = "模型回复不是可解析的 JSON"
                continue  # 坏格式重试（上限封顶，防烧钱）
            if not parsed:
                return ExtractResult(saved=False, attempts=attempt, error="")
            path = append_pending(
                self._root,
                session_id=session_id,
                title=title,
                lines=_render_pending_lines(parsed),
                day=day,
            )
            return ExtractResult(
                saved=True,
                saved_count=len(parsed),
                attempts=attempt,
                path=str(path),
                titles=[str(m["title"]) for m in parsed],
            )

        return ExtractResult(
            saved=False,
            attempts=attempt,
            error=last_error,
        )
