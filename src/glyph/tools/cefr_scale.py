"""cefr_scale：CEFR 写作 can-do 量表（内置）+ 单词 CEFR 等级近似。

- ``CEFR_WRITING_CAN_DO`` 是欧洲语言共同参考框架（CEFR, Council of Europe 发布）
  "总体书面产出 / Overall Written Production" 各级能力描述的**中文概述**，作为
  模型对照的公开依据；它是能力描述、不是评分细则，故只用于"对照"，最终给的是
  等级区间而非精确分数。
- ``estimate_word_level`` 用 ECDICT 的考试标签 / 词频 / 牛津标记近似单词等级。
  ECDICT 不含官方 CEFR 词汇表（English Vocabulary Profile 需授权），所以这是
  **近似**，调用方必须在 warnings 里向用户说明，不能当作权威 CEFR 词汇判定。
"""

from __future__ import annotations

__all__ = [
    "CEFR_LEVELS",
    "CEFR_WRITING_CAN_DO",
    "estimate_word_level",
    "level_distribution",
]

CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
_LEVEL_INDEX = {lvl: i for i, lvl in enumerate(CEFR_LEVELS)}

# CEFR "Overall Written Production" 各级能力的中文概述（据 Council of Europe CEFR）
CEFR_WRITING_CAN_DO: dict[str, str] = {
    "A1": (
        "能写最简单的孤立短语和句子：写明信片上的固定套话、"
        "在表格里填写个人基本信息（姓名、地址、国籍）。"
    ),
    "A2": (
        "能就家庭、日常生活、个人经历等熟悉话题写简短、简单的便条和短文，"
        "能写简单的私人信件（道谢、致歉、邀约）。"
    ),
    "B1": (
        "能就熟悉或个人感兴趣的主题写连贯的简单短文，能描述经历、感受、"
        "计划与理由，能写带观点的私人信件。"
    ),
    "B2": (
        "能就较广的话题写清晰、详细的文章，能写评论或报告、陈述并论证观点，"
        "能针对不同读者调整语气、有效衔接。"
    ),
    "C1": (
        "能就复杂话题写结构清晰、表达得体的文章、报告或评论，"
        "能合理组织长文、熟练运用衔接与多样句式，风格一致。"
    ),
    "C2": (
        "能写清晰流畅、完全得体的复杂文体（正式报告、评论、文学性文字），"
        "逻辑结构精巧、能引导读者把握要点。"
    ),
}

# ECDICT 考试标签 → 掌握该词大致对应的 CEFR 等级（近似）
_TAG_LEVEL = {
    "cet4": "B1",
    "cet6": "B2",
    "ky": "B2",
    "ielts": "C1",
    "toefl": "C1",
    "gre": "C2",
}


def estimate_word_level(*, tag: str | None, frq: int | None, oxford: int | None) -> str:
    """用 ECDICT 信号近似一个已收录词的 CEFR 等级。

    综合多个信号，**冲突时从低判**（宁可把常见词判简单，也不误判为难词）；
    完全没有信号时返回 "C1" 作为保守兜底，调用方应同时把这类词计入"无依据"。
    """
    levels: list[str] = []
    if tag:
        for t in tag.split():
            if t in _TAG_LEVEL:
                levels.append(_TAG_LEVEL[t])
    if frq:
        if frq <= 1500:
            levels.append("A2")
        elif frq <= 3500:
            levels.append("B1")
        elif frq <= 7000:
            levels.append("B2")
        elif frq <= 12000:
            levels.append("C1")
        else:
            levels.append("C2")
    if oxford:
        levels.append("B1")  # 牛津核心 3000 多为 A1–B1
    if not levels:
        return "C1"
    return min(levels, key=_LEVEL_INDEX.get)


def level_distribution(word_levels: list[str | None]) -> dict:
    """把逐词等级（None=未收录）聚合成各级计数/占比，外加未收录统计。"""
    counts = {lvl: 0 for lvl in CEFR_LEVELS}
    unknown = 0
    for lvl in word_levels:
        if lvl is None:
            unknown += 1
        else:
            counts[lvl] += 1
    total = len(word_levels)

    def pct(n: int) -> float:
        return round(n / total * 100, 1) if total else 0.0

    return {
        "total": total,
        "counts": counts,
        "percent": {lvl: pct(counts[lvl]) for lvl in CEFR_LEVELS},
        "unknown_count": unknown,
        "unknown_percent": pct(unknown),
    }
