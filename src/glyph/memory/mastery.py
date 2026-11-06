"""掌握度账本：状态机纯函数（第二棒，无 IO、无模型）。

把"这个知识点掌握到哪一步、下次什么时候复测、连对几次算掌握"做成**确定性规则**，
而不是让模型临场判断。模型只负责出题与判档（语言活），档位推进、到期、销账、复活
一律走这里的纯函数（硬事实），可单测、可留痕、模型不能空口销账。

默认乙套餐（``DEFAULT_SCHEDULE``）：建档次日首考 → 第 1 次独立做对排 3 天后 →
第 2 次排 7 天后 → 第 3 次独立做对即掌握（连对 3 次，最快约 11 天）。提示后对不升级
也不清零、次日再来；没做对连对清零、打回起点、次日重来。逾期没来/拒绝不惩罚不销账。
掌握之后又在新作文犯同一采纳硬错，``reopen`` 自动复活、清零重走阶梯。

知识点用 ERRANT 受控标签（``error_code``，如 ``VERB:TENSE``），与批改记忆里的粗桶
``issue_type``（hard/chinglish/wordy）是两回事，不能混用。

所有函数不做 IO、today 一律由参数注入，便于离线单测与重放。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum

from .types import MemoryEntry, MemoryType

__all__ = [
    "CODE_LABELS_ZH",
    "DEFAULT_SCHEDULE",
    "MasteryPoint",
    "MasteryStage",
    "ReviewVerdict",
    "ScheduleConfig",
    "VALID_ERROR_CODES",
    "apply_point_to_entry",
    "enroll",
    "is_due",
    "normalize_error_code",
    "point_from_entry",
    "record_verdict",
    "reopen",
    "select_due",
]


class MasteryStage(StrEnum):
    """掌握度阶段。"""

    NEW = "new"  # 刚攒下、还没首考
    LEARNING = "learning"  # 阶梯进行中
    MASTERED = "mastered"  # 连对达标、已掌握，不再主动提醒


class ReviewVerdict(StrEnum):
    """一次复测的三档判定（模型判、用户可推翻）。"""

    INDEPENDENT = "independent"  # 独立做对
    HINTED = "hinted"  # 提示后才对
    WRONG = "wrong"  # 没做对


#: 受控知识点（ERRANT 子集）的人可读中文名，用于到期注入等展示；
#: 不在表内的代码回退显示代码本身（自造标签已由写入侧白名单拦截）。
CODE_LABELS_ZH: dict[str, str] = {
    "VERB:TENSE": "时态",
    "MORPH": "词形（三单/比较级/词性转换）",
    "NOUN:NUM": "名词单复数",
    "NOUN:INFL": "可数/不可数词形",
    "DET": "冠词",
    "SPELL": "拼写",
}

#: 受控知识点白名单，由中文名表的键固化（单一事实源），批改侧与记忆侧共用：
#: 只有在这个集合里的代码才会建账、安排复测。
VALID_ERROR_CODES: frozenset[str] = frozenset(CODE_LABELS_ZH)


def normalize_error_code(raw: object) -> str | None:
    """把模型 / 工具给的知识点代码规范化到白名单；不在 6 类内（含空值）返回 None。

    容错只做两件无害的事：去首尾空白、整体大写（本子集标签全大写，模型给
    ``"verb:tense"`` 也认）。介词 PREP、标点、句子结构、自造 / 乱码标签一律 None ——
    它们这一版"只批改、不追踪"（决策 D17）。非字符串（None / 数字等）同样返回 None。
    """
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper()
    return code if code in VALID_ERROR_CODES else None


@dataclass(frozen=True)
class ScheduleConfig:
    """复习阶梯参数（改数字即可切换套餐）。

    ``good_intervals`` 是"第 k 次独立做对后，隔几天再考"，长度必须等于
    ``mastery_streak - 1``：最后一次独立做对即掌握、不再排间隔。
    """

    first_review_days: int = 1
    good_intervals: tuple[int, ...] = (3, 7)
    mastery_streak: int = 3
    retry_days: int = 1

    def __post_init__(self) -> None:
        if len(self.good_intervals) != self.mastery_streak - 1:
            raise ValueError(
                "good_intervals 长度必须等于 mastery_streak - 1："
                f"streak={self.mastery_streak} 需要 {self.mastery_streak - 1} 个间隔，"
                f"实际 {len(self.good_intervals)} 个"
            )
        if self.first_review_days < 0 or self.retry_days < 0:
            raise ValueError("复习间隔不能为负")


# 乙套餐：次日首考 → 1→3→7 天 → 连对 3 次即掌握
DEFAULT_SCHEDULE = ScheduleConfig(
    first_review_days=1,
    good_intervals=(3, 7),
    mastery_streak=3,
    retry_days=1,
)


@dataclass(frozen=True)
class MasteryPoint:
    """一个知识点（ERRANT 标签）的掌握度状态。值对象：推进一律返回新对象。"""

    key: str  # 记忆条目 key（与 mistake 记忆一一对应）
    error_code: str  # ERRANT 受控标签，如 VERB:TENSE
    first_seen: date
    stage: MasteryStage = MasteryStage.NEW
    streak: int = 0  # 当前连续独立做对次数
    due_date: date | None = None  # 下次到期日；None = 已掌握不再提醒
    good_count: int = 0  # 累计独立做对
    hard_count: int = 0  # 累计提示后对
    wrong_count: int = 0  # 累计没做对
    last_review_at: date | None = None
    reopened_count: int = 0
    #: D31：怎么出的师——"streak"（复测连对达标）或 "user_claim"（用户自述 + 出题验证通过）。
    #: 两种都算数；留这个字段是为了以后回看时能分清这条结论依据有多硬。
    mastered_by: str | None = None


def enroll(
    key: str,
    error_code: str,
    today: date,
    *,
    config: ScheduleConfig = DEFAULT_SCHEDULE,
) -> MasteryPoint:
    """定稿采纳硬错后建档：阶段 new，次日首考。"""
    return MasteryPoint(
        key=key,
        error_code=error_code,
        first_seen=today,
        stage=MasteryStage.NEW,
        streak=0,
        due_date=today + timedelta(days=config.first_review_days),
    )


def is_due(point: MasteryPoint, today: date) -> bool:
    """到期且未掌握。逾期也算到期（``due_date <= today``）。"""
    if point.stage is MasteryStage.MASTERED or point.due_date is None:
        return False
    return point.due_date <= today


def record_verdict(
    point: MasteryPoint,
    verdict: ReviewVerdict,
    today: date,
    *,
    config: ScheduleConfig = DEFAULT_SCHEDULE,
) -> MasteryPoint:
    """记录一次复测判定，返回推进后的新状态（不改入参）。

    - independent：连对 +1；达到 ``mastery_streak`` 掌握，否则按阶梯排下一次；
    - hinted：不升级、连对不增也不清零，停在当前档、``retry_days`` 后再来；
    - wrong：连对清零、回 learning、``retry_days`` 后重来。

    已掌握的点不能直接记录（必须先 ``reopen``），否则抛 ValueError —— 堵死绕过
    复活流程、对已掌握点继续推进的非法路径。
    """
    if point.stage is MasteryStage.MASTERED:
        raise ValueError("已掌握的点不能直接记录复测，请先 reopen 复活")

    if verdict is ReviewVerdict.INDEPENDENT:
        new_streak = point.streak + 1
        if new_streak >= config.mastery_streak:
            return replace(
                point,
                stage=MasteryStage.MASTERED,
                streak=new_streak,
                due_date=None,
                good_count=point.good_count + 1,
                last_review_at=today,
                mastered_by="streak",  # D31：复测连对达标为已掌握
            )
        interval = config.good_intervals[new_streak - 1]
        return replace(
            point,
            stage=MasteryStage.LEARNING,
            streak=new_streak,
            due_date=today + timedelta(days=interval),
            good_count=point.good_count + 1,
            last_review_at=today,
        )

    if verdict is ReviewVerdict.HINTED:
        # 首次首考"提示后对"：离开 new 进入 learning，但连对仍为 0，不沿阶梯前进
        return replace(
            point,
            stage=MasteryStage.LEARNING,
            due_date=today + timedelta(days=config.retry_days),
            hard_count=point.hard_count + 1,
            last_review_at=today,
        )

    # wrong：清零、回起点、次日重来
    return replace(
        point,
        stage=MasteryStage.LEARNING,
        streak=0,
        due_date=today + timedelta(days=config.retry_days),
        wrong_count=point.wrong_count + 1,
        last_review_at=today,
    )


def record_user_claim(
    point: MasteryPoint,
    today: date,
    *,
    verified: bool = True,
    config: ScheduleConfig = DEFAULT_SCHEDULE,
) -> MasteryPoint:
    """用户说"我会了"这条路（D31）：出一道题验证，**答对才算掌握**。

    设计取舍（项目所有者 2026-10-15 拍板）：
    - 完全不信 → 用户烦（"我都说了我会了"）；
    - 完全信 → 容易被高估，而且他以后再犯时系统认不出是复发；
    - **出一道题** → 比"连对 3 次"友好得多，又比"直接信"可靠。

    verified=False（答错 / 没验）时**保持原状态**——连对不加、
    good_count 不加、due_date 不动，阶梯照原样继续。

    与 record_verdict 的区别：那条是"复测判档"的常规路径
    （掌握方式记 streak）；这条是"用户自述"的快捷路径（记 user_claim），
    两者都进 MasteryStage.MASTERED，但以后回看能分清依据。
    """
    if not verified:
        return point
    if point.stage is MasteryStage.MASTERED:
        return point  # 已经掌握了，不重复记账
    return replace(
        point,
        stage=MasteryStage.MASTERED,
        streak=point.streak + 1,
        due_date=None,
        good_count=point.good_count + 1,
        last_review_at=today,
        mastered_by="user_claim",
    )

def reopen(point: MasteryPoint, today: date, *, config: ScheduleConfig = DEFAULT_SCHEDULE):
    """掌握之后又犯同一采纳硬错：复活回 learning、连对清零、次日重走阶梯。

    保留最初发现日与历史 good_count（只是没真正掌握，不是没学过），复活次数 +1。
    """
    return replace(
        point,
        stage=MasteryStage.LEARNING,
        streak=0,
        due_date=today + timedelta(days=config.first_review_days),
        reopened_count=point.reopened_count + 1,
        last_review_at=today,
        mastered_by=None,  # D31：复活后不再是"已掌握"
    )


def select_due(
    points: list[MasteryPoint],
    today: date,
    *,
    limit: int = 1,
) -> list[MasteryPoint]:
    """选出今天该复测的点，最多 ``limit`` 个（默认 1 个）。

    排序：错得最多优先（``wrong_count`` 多）→ 拖最久没复习（``due_date`` 早）→
    最早发现（``first_seen`` 早）→ key 兜底，保证确定、可复现。
    """
    due = [p for p in points if is_due(p, today)]
    due.sort(
        key=lambda p: (
            -p.wrong_count,
            p.due_date or date.max,
            p.first_seen,
            p.key,
        )
    )
    return due[:limit]


def point_from_entry(entry: MemoryEntry) -> MasteryPoint | None:
    """把一条**已建档**的 mistake 记忆还原成掌握度点；否则返回 ``None``。

    用于到期注入：只有 v2、带 ``mastery_stage`` 与 ``error_code`` 的错题才进复测队列；
    v1 旧记忆（字段缺失）安全地读成 None，不会被自动拉进复测。阶段/代码/首次发现日
    缺失或日期坏掉就放弃该点（宁可不提醒，也不基于脏数据推进）；计数等增强字段缺失
    按 0 处理，失败方向偏保守（宁可多复习，也不会错误地把人判成已掌握）。
    """
    if entry.type is not MemoryType.MISTAKE or entry.mastery_stage is None:
        return None
    code = entry.error_code
    if not code:
        return None
    try:
        stage = MasteryStage(entry.mastery_stage)
        first_seen = date.fromisoformat(entry.first_seen)
        due = date.fromisoformat(entry.due_date) if entry.due_date else None
        last_review = date.fromisoformat(entry.last_review_at) if entry.last_review_at else None
    except ValueError:
        return None
    return MasteryPoint(
        key=entry.key,
        error_code=code,
        first_seen=first_seen,
        stage=stage,
        streak=entry.independent_streak or 0,
        due_date=due,
        good_count=entry.good_count or 0,
        hard_count=entry.hard_count or 0,
        wrong_count=entry.wrong_count or 0,
        last_review_at=last_review,
        reopened_count=entry.reopened_count or 0,
        mastered_by=entry.mastered_by,
    )


def apply_point_to_entry(point: MasteryPoint, entry: MemoryEntry) -> MemoryEntry:
    """把推进后的掌握度点写回记忆条目**副本**（``point_from_entry`` 的逆操作）。

    记账工具落盘前调用：只覆盖账本字段（``error_code`` + 9 个掌握度标量，含 mastered_by），
    身份 / 正文 / ``count`` / 三个日期等其它内容原样保留，返回新条目、不改入参。
    模型拿不到这些字段的写入口——档位只能由状态机纯函数算出后，经这里回写，
    从根上杜绝"模型空口销账 / 改到期日"。
    """
    return replace(
        entry,
        error_code=point.error_code,
        mastery_stage=point.stage.value,
        independent_streak=point.streak,
        due_date=point.due_date.isoformat() if point.due_date is not None else None,
        good_count=point.good_count,
        hard_count=point.hard_count,
        wrong_count=point.wrong_count,
        last_review_at=(
            point.last_review_at.isoformat() if point.last_review_at is not None else None
        ),
        reopened_count=point.reopened_count,
        # D31：怎么出的师（streak / user_claim）。复活时状态机已清空它，
        # 这里如实跟着走，不保留旧值。
        mastered_by=point.mastered_by,
    )
