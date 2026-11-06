"""T06：学习状态摘要的上下文片段（只读）。

桌面端的词书 / SRS 卡片 / 错词都在浏览器本地，Agent 本来看不见，
于是它能批改变查词，却不能依据真实的背词进度安排下一步。
这一段把"今天到期多少、新学复习各做多少、哪些词老忘"用几行字告诉它。

三条边界（与前端 ``adapters/studyContext.ts`` 一一对应）：
1. **只读**。只渲染事实供参考；不允许 Agent 改 SRS / 计划 ——
   所以渲染结果里**不出现** ``reps`` / ``lapses`` / ``dueDate`` 这类可回写字段。
2. **没数据就不渲染**。返回 None，``render_context_message`` 会跳过这一段；
   Agent 看不到摘要时按提示词要求说"不知道"，不许自己编进度。
3. **没有的项目就不写**。没有反复遗忘的词就整行省略，
   不写"无"——"没有记录"和"查过是零"是两回事，混起来就是在编。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NotRequired, TypedDict

from .inject import ContextFragment

__all__ = ["StudyContext", "render_study_context", "study_fragment"]


class StudyContext(TypedDict):
    """前端算好学状态后交给后端的那一份。字段刻意少，够用就停。"""

    book: str
    mode: str
    due: NotRequired[int]
    newTotal: NotRequired[int]
    newDone: NotRequired[int]
    reviewTotal: NotRequired[int]
    reviewDone: NotRequired[int]
    hardWords: NotRequired[list[str]]


def render_study_context(ctx: StudyContext | None) -> str | None:
    """渲染成一段正文；没有数据返回 None（调用方据此不追加）。"""
    if not ctx:
        return None
    book = str(ctx.get("book") or "").strip()
    if not book:
        return None

    lines = [
        "以下是背单词模块的真实进度（**只读**，只供你参考；"
        "改进度、改计划仍由背词页或正式写入能力完成，你不能改）：",
    ]

    due = ctx.get("due")
    if isinstance(due, int) and due > 0:
        lines.append(f"- 今日到期：{due} 个")

    new_total = ctx.get("newTotal")
    new_done = ctx.get("newDone")
    if isinstance(new_total, int) and new_total > 0:
        lines.append(f"- 今日新学：{new_done if isinstance(new_done, int) else 0}/{new_total}")

    review_total = ctx.get("reviewTotal")
    review_done = ctx.get("reviewDone")
    if isinstance(review_total, int) and review_total > 0:
        lines.append(
            f"- 今日复习：{review_done if isinstance(review_done, int) else 0}/{review_total}"
        )

    hard = ctx.get("hardWords") or []
    if hard:
        words = "、".join(str(w) for w in hard[:5])
        lines.append(f"- 反复遗忘的词：{words}（按遗忘次数排）")

    # 只有一句"当前词书"也要带上，否则 Agent 连在背哪本都不知道
    lines.insert(1, f"- 当前词书：{book}")

    return "<study_context>\n" + "\n".join(lines) + "\n</study_context>"


def study_fragment(getter: Callable[[], StudyContext | None]) -> ContextFragment:
    """做成回调：数据在请求之间会变（用户刚背完几个词），每轮都要取最新的。"""

    def fragment() -> str | None:
        return render_study_context(getter())

    return fragment
