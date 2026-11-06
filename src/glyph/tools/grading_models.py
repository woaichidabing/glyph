"""文本批改的领域模型。

一条 Detection = 原文中的一个问题点及其修改建议。位置（start/end）是相对
**原始输入文本**的半开字符区间 ``[start, end)``；定位不到时为 ``None`` 且
``aligned=False``，条目仍保留（前端可放进"其他建议"区）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 依赖方向是单向的：grading_models → context（反过来会成环）
from .context import ContextScope

# hard：拼写/语法/标点等确定错误；chinglish：中式英语/不地道；wordy：冗余
IssueType = Literal["hard", "chinglish", "wordy"]
# error：确定错；suggestion：可改可不改
Tier = Literal["error", "suggestion"]
# model：批改模型产出；spell：本地拼写库产出
Source = Literal["model", "spell"]
# 复核三态：维持原判 / 撤销 / 改判
RecheckStatus = Literal["upheld", "withdrawn", "revised"]


class RecheckInfo(BaseModel):
    """一条被驳回的条目经过复核后的结论（**只作展示与裁决依据**）。

    挂在原条目上，**不新建条目、不换 id**：使用者刚驳回的卡片不能凭空消失或冒出新卡，
    「本篇 N 处问题」也不因复核而虚增。

    - ``upheld`` 维持原判：AI 复核后仍认为这里有错 → 回到待裁决，由使用者再拍板；
    - ``withdrawn`` 撤销：AI 认错 → 不进定稿，界面灰显让使用者知道自己驳对了；
    - ``revised`` 改判：AI 换了改法或换了定性 → 带新建议回到待裁决。

    三态而不是二态：只给「维持 / 撤销」的话，AI 复核后想换个改法就没有出口，
    会被迫维持一个它自己都觉得不对的改法。
    """

    status: RecheckStatus
    round: int = 1
    scope: ContextScope = "sentence"
    # 改判时才给新的改法 / 讲解 / 定性；维持原判时回带原值；撤销时一律 None
    corrected: str | None = None
    explanation_zh: str | None = None
    tier: Tier | None = None
    type: IssueType | None = None
    # 给使用者看的固定文案（由常量模板生成，三态必非空，不依赖模型输出）
    note_zh: str = ""
    # 模型给的一句理由（可空）
    reason_zh: str = ""


class Detection(BaseModel):
    """单个问题点。

    ``recheck`` 是**只增不改**的字段：``grade_text`` 产出时恒为 ``None``，只有条目
    被驳回后经 ``recheck_detection`` 复核才会挂上结论（三态之一）。留这个字段是为了
    「驳回 → 复核」全程**不换 id、不新建条目**——使用者刚驳回的卡片不能凭空消失或错位。
    """

    original: str = ""
    corrected: str = ""
    type: IssueType = "hard"
    tier: Tier = "error"
    explanation_zh: str = ""
    source: Source = "model"
    # ERRANT 知识点代码（白名单 6 类，见 memory.mastery.VALID_ERROR_CODES）；
    # 仅"最终裁决 hard+error 且落在白名单内"才有值，None = 不进掌握度账本、不安排复测。
    error_code: str | None = None
    # 相对原始输入文本的字符区间；对齐失败为 None
    start: int | None = None
    end: int | None = None
    aligned: bool = False
    # 合并、限条后按展示顺序分配的稳定编号（d1、d2…），供以后单条驳回引用
    id: str | None = None
    # 复核结论（grade_text 产出时为 None，复核后才有值）。只读展示 + 裁决依据，
    # 不参与 merge.py 的限条 / 去重 / 编号计算。
    recheck: RecheckInfo | None = None


class DimensionScore(BaseModel):
    """单个评分维度。

    ``score`` 为 0 到 100 的整数；**不适用时为 None**（例如只批一句话，
    谈不上"篇章结构"，该维就留空而不是硬凑一个分）。``comment_zh`` 是一句
    简体中文点评：先说这一维现状，再给一条最可操作的提升建议。
    """

    score: int | None = None
    comment_zh: str = ""


class WritingScores(BaseModel):
    """写作四维评分：内容 / 结构 / 准确 / 丰富。

    - content 内容：切题、观点清楚与充分、有没有展开和支撑；
    - structure 结构：连贯衔接、段落与逻辑顺序、开头结尾；
    - accuracy 准确：语法 / 拼写 / 用词 / 标点的正确率（错误越多越重分越低）；
    - richness 丰富：词汇广度与搭配地道度、句式是否多样。
    """

    content: DimensionScore = Field(default_factory=DimensionScore)
    structure: DimensionScore = Field(default_factory=DimensionScore)
    accuracy: DimensionScore = Field(default_factory=DimensionScore)
    richness: DimensionScore = Field(default_factory=DimensionScore)


class GradeResult(BaseModel):
    """一次整篇批改的结构化成品（grade_text 工具 output 的核心）。"""

    detections: list[Detection] = Field(default_factory=list)
    summary: str = ""
    # 四维评分；旧模型 / 结果截断没给分时为 None——分数是增强，绝不能因为
    # 没出分就把整次批改废掉（detections 才是核心）。
    scores: WritingScores | None = None


def effective_error_code(detection: Detection) -> str | None:
    """这条检测**最终**还带不带可追踪的知识点代码（决策 D17）。

    代码只对"最终裁决为 hard+error"的条目有效，把驳回复核的结果也算进去：

    - 没复核、或复核维持（``upheld``）：看顶层定性，hard+error 才返回 ``error_code``；
    - 复核撤销（``withdrawn``）：整条判断不成立，返回 None；
    - 复核改判（``revised``）：以改判后的定性为准（``RecheckInfo`` 已回带最终值），
      仍是 hard+error 才沿用原代码（复核第一版不重新产码），否则 None。
    """
    recheck = detection.recheck
    if recheck is not None and recheck.status == "withdrawn":
        return None
    if recheck is not None and recheck.status == "revised":
        final_type = recheck.type if recheck.type is not None else detection.type
        final_tier = recheck.tier if recheck.tier is not None else detection.tier
    else:
        final_type, final_tier = detection.type, detection.tier
    if final_type != "hard" or final_tier != "error":
        return None
    return detection.error_code
