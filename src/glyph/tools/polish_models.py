"""润色升档（polish_text）的领域模型。

一条 ``Suggestion`` = 原文里一处"没有错、但可以更好"的地方，配两个版本：

- ``safe``：现阶段稳妥可用的说法（保证不出错，改动小）；
- ``advanced``：往上一级靠的说法（更精确 / 更紧凑 / 更得体）。

两档而不是三档是刻意的：三档会让用户陷入"该挑哪个"的选择负担，两档才是教学梯度。

``Upgrade`` 里的 ``tags`` / ``level_approx`` **一律由本地词库查出**（``polish_rules``
负责填），模型只给 ``source`` / ``target`` 词对。理由：模型说得出"这词更高级"，
却说不出"这词属于 CET6 还是雅思"——那份数据在 ``data/dict.db`` 里，不在模型的语感里。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Upgrade", "Suggestion", "PolishDraft"]


@dataclass(frozen=True)
class Upgrade:
    """一处用词升级：``source`` → ``target``。

    ``tags`` 是 ECDICT 的**硬标签**（CET4 / CET6 / 考研 / 雅思…），可以直接展示；
    ``level_approx`` 是据标签与词频**近似**出的 CEFR 等级，展示时必须带"约"字样。
    查不到（短语、专有名词）时两者都留空，**绝不编造**。
    """

    source: str
    target: str
    tags: list[str] = field(default_factory=list)
    level_approx: str | None = None


@dataclass(frozen=True)
class Suggestion:
    """一处升档建议。``start`` / ``end`` 是由 ``polish_rules`` 对齐出来的原文位置。"""

    original: str
    issue: str  # 给用户看的一句话问题描述（如"词太泛"）
    issue_kind: str  # 枚举：word_precision / sentence_tightness / register
    safe: str
    advanced: str
    why: str
    upgrades: list[Upgrade] = field(default_factory=list)
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class PolishDraft:
    """模型一次润色的原始产出（尚未做本地的对齐 / 定级）。"""

    suggestions: list[Suggestion] = field(default_factory=list)
    overall: str = ""
    nothing_to_polish: bool = False
