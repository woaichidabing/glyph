"""记忆正文的确定性渲染与解析。

同字段必得同文件（渲染是唯一写入路径）；解析是渲染的逆操作，按 ``##`` 小节标题
取值。正文结构见设计稿 §3.3 字段表。
"""

from __future__ import annotations

from .types import MemoryEntry, MemoryType, TimeHint

__all__ = ["render_body", "parse_body_sections"]

_QUOTE = "用户原话"


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.strip().split("\n"))


_STAGE_ZH = {"new": "刚攒下", "learning": "学习中", "mastered": "已掌握"}


def _render_mastery(entry: MemoryEntry) -> str:
    """渲染掌握度账本小节（给人看；状态真相在 frontmatter，不从这里回读）。"""
    stage = _STAGE_ZH.get(entry.mastery_stage or "", entry.mastery_stage or "")
    next_due = entry.due_date or "—（已掌握）"
    return "\n".join(
        [
            f"知识点代码：{entry.error_code or '—'}",
            f"状态：{stage}（独立连对 {entry.independent_streak or 0} 次）",
            f"下次复习：{next_due}",
            f"累计：独立对 {entry.good_count or 0} · 提示后对 "
            f"{entry.hard_count or 0} · 没做对 {entry.wrong_count or 0}",
            f"最近复测：{entry.last_review_at or '还没测过'} · 复活 {entry.reopened_count or 0} 次",
        ]
    )


def render_body(entry: MemoryEntry) -> str:
    """从字段确定性渲染正文。"""
    parts = [f"# {entry.title}", "", f"## {_QUOTE}", _blockquote(entry.quote)]

    if entry.type is MemoryType.MISTAKE:
        parts += [
            "",
            "## 原文片段",
            (entry.original or "").strip(),
            "",
            "## 建议改法",
            (entry.correction or "").strip(),
        ]
        if entry.explanation:
            parts += ["", "## 讲解", entry.explanation.strip()]
        if entry.mastery_stage is not None:
            parts += ["", "## 掌握度", _render_mastery(entry)]
    elif entry.type is MemoryType.FEEDBACK:
        parts += ["", "## Why", (entry.why or "").strip()]
        parts += ["", "## How to apply", (entry.how_to_apply or "").strip()]
    elif entry.type is MemoryType.FACT and entry.time is not None:
        time = entry.time
        if time.exact is not None:
            converted = f"精确 {time.exact}"
        else:
            approx = "（模糊）" if time.approximate else ""
            converted = f"区间 {time.span_start} ~ {time.span_end}{approx}"
        parts += ["", "## 时间", f"用户原话：{time.quote}", converted]

    return "\n".join(parts).strip() + "\n"


def parse_body_sections(body: str) -> dict[str, str]:
    """按 ``## 标题`` 把正文拆成 {标题: 内容}；引用块去掉 ``> `` 前缀。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in body.split("\n"):
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line[2:] if line.startswith("> ") else line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def build_time_hint(fields: dict[str, str], sections: dict[str, str]) -> TimeHint | None:
    """从 frontmatter 字段重建 TimeHint；缺关键信息返回 None。"""
    quote = fields.get("time_quote") or sections.get("时间", "").split("\n")[0].replace(
        "用户原话：", ""
    )
    exact = fields.get("time_exact")
    start = fields.get("time_span_start")
    end = fields.get("time_span_end")
    approx = fields.get("time_approx") == "true"
    try:
        if exact:
            return TimeHint(quote=quote, exact=exact)
        if start and end:
            return TimeHint(quote=quote, span_start=start, span_end=end, approximate=approx)
    except ValueError:
        return None
    return None
