"""润色升档的**确定性规则**（纯函数，离线可测）：锚点对齐 / 去重 / 限条 / 升级词定级。

设计原则与 ``merge.py`` 一致：模型只负责"哪里可以更好、怎么改"，所有位置计算、
数量控制、词级标注都在这里做——**能算的不要问模型**。

四条规则各自防一个已知的失败模式：

1. **锚点对齐**：模型会返回原文里根本不存在的"原句"，用户对照时找不到位置 → 丢弃；
2. **去重**：同一处被挑两遍、或两条建议位置嵌套重叠 → 只留第一条；
3. **限条**：条数超上限时模型必然开始凑数 → 硬截断并如实记 warning；
4. **定级**：等级一律查本地词库，查不到就留空——**绝不编**。
"""

from __future__ import annotations

from dataclasses import replace

from .polish_models import Suggestion, Upgrade

__all__ = [
    "MAX_SUGGESTIONS",
    "ISSUE_KINDS",
    "normalize_issue_kind",
    "annotate_upgrades",
    "finalize_suggestions",
]

# 单次最多给几处（**单点定义**，R4）。再多用户读不完，且模型为了填满条数会开始硬凑
# （谄媚的温床）。提示词与工具描述里的同一个数字由同步测试守住，不靠注释提醒。
MAX_SUGGESTIONS = 5

# 升档三类口径（**单点定义**，R4）。归不进这三类的改动一律不该给——
# "换了但没更好"不算升档。
#
# 提示词里必须照抄这三个值（那是给模型的自然语言，不宜用 f-string 插值把可读性
# 打成花括号地狱），「两边不许漂移」由 tests/tools/test_polish_prompt.py 的同步测试守住。
ISSUE_KINDS = ("word_precision", "sentence_tightness", "register")
_DEFAULT_KIND = "word_precision"

_CLIP = 40


def _clip(text: str) -> str:
    """warning 里引用片段时截断，避免把一整句塞进提示。"""
    t = (text or "").strip().replace("\n", " ")
    return t if len(t) <= _CLIP else f"{t[:_CLIP]}…"


def normalize_issue_kind(raw: object) -> str:
    """把 ``issue_kind`` 收进枚举；未知值归到默认档，**不透传**给前端。

    刻意静默、不报 warning：提示词已把三值写死，模型越界的概率很低，为一次越界
    把 warning 一路穿到工具层，链路成本高于收益。归到 ``word_precision`` 是三个里
    语义最宽的（"用词可以更精确"），最不容易与原文的事实冲突。
    """
    if isinstance(raw, str) and raw.strip() in ISSUE_KINDS:
        return raw.strip()
    return _DEFAULT_KIND


def annotate_upgrades(upgrades: list[Upgrade], repo) -> list[Upgrade]:
    """给升级词补上本地词库的考试标签与近似等级。

    ``repo`` 为 ``DictRepository``（或 None 表示没有词库可用）。查不到就留空：
    短语、专有名词、超纲词都查不到，这是常态，不是错误。
    """
    out: list[Upgrade] = []
    for up in upgrades:
        tags: list[str] = []
        level: str | None = None
        if repo is not None and up.target:
            entry = repo.lookup(up.target)
            if entry:
                tags = list(entry.get("tags") or [])
                level = repo.word_level(up.target)
        out.append(Upgrade(source=up.source, target=up.target, tags=tags, level_approx=level))
    return out


def finalize_suggestions(
    suggestions: list[Suggestion], text: str
) -> tuple[list[Suggestion], list[str]]:
    """对齐 / 去重 / 限条，返回 ``(保留的建议, warnings)``。

    对齐失败与超限都**如实记 warning**，不静默吞掉——用户有权知道
    "模型给的有一处对不上原文"或"超过上限被截了"。
    """
    warnings: list[str] = []
    kept: list[Suggestion] = []
    seen: set[str] = set()
    spans: list[tuple[int, int]] = []

    for sug in suggestions:
        pos = text.find(sug.original)
        if pos < 0:
            warnings.append(f"有一处建议在原文里找不到对应句子，已丢弃：{_clip(sug.original)}")
            continue

        start, end = pos, pos + len(sug.original)
        key = sug.original.strip().lower()
        if key in seen:
            continue  # 同一处被挑了两遍
        if any(not (end <= s or start >= e) for s, e in spans):
            continue  # 与已保留的建议位置重叠（嵌套片段）
        seen.add(key)
        spans.append((start, end))
        kept.append(replace(sug, start=start, end=end))

    if len(kept) > MAX_SUGGESTIONS:
        warnings.append(
            f"可升档的地方超过上限，本次只保留最靠前的 {MAX_SUGGESTIONS} 处"
            f"（单次最多 {MAX_SUGGESTIONS} 处）"
        )
        kept = kept[:MAX_SUGGESTIONS]

    return kept, warnings
