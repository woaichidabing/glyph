"""逐条裁决与定稿：**纯函数，不联网、不调模型、无状态**。

闸门②（逐条拍板）的后端落地就在这里：

* ``apply_decisions()`` —— 使用者对着 d1、d2、d3 逐一表态后，算出**定稿条目**；
* ``finalize_text()`` —— 把定稿条目按字符区间套回原文，产出**改后全文**；
* ``build_marked_segments()`` —— 把批改痕迹**印回原文**，产出分段序列（批阅稿）；
* ``build_result_text()`` —— 把定稿条目拼成一份可直接交出去的**清单**文本。

为什么它不是一个 Tool（也不注册进 ``ToolRegistry``）：R2 约束的是"**可被模型调用的能力**"，
而拍板是**人的动作**——模型不在场，也不该在场（定稿只由人的选择决定，模型不再插嘴）。
因此这里做成纯函数不违反 R2，也天然不碰 ``loop.py``，``runtime/`` 保持业务无关。

为什么要"纯函数"：这俩函数是整条批改链里**唯一能离线百分百验证**的部分，
不给它留任何网络与状态，测试就能把"定稿口径"钉死。
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from pydantic import BaseModel

from .grading_models import Detection

# accepted 采纳 ｜ edited 改成我的（人自己写改法）｜ rejected 驳回 ｜ cancelled 整轮放弃
Decision = Literal["accepted", "rejected", "edited", "cancelled"]

# 进定稿的只有这两种：认可 AI 的改法 / 人自己给的改法
_KEPT: frozenset[str] = frozenset({"accepted", "edited"})


class Mark(BaseModel):
    """批阅稿里的一小段：原文 / 被划掉的错处 / 顶上去的改法。

    前端按顺序渲染就能得到"批改痕迹印在作文上"的效果：``plain`` 原样输出、
    ``removed`` 加删除线、``added`` 标绿并挂上标编号。

    * ``index`` / ``detection_id`` **只有 added 段有**：编号按**原文位置**从 1 起，
      与 ``build_result_text`` 的清单编号一一对应——两处编号必须指同一条，
      否则使用者对着批阅稿看讲解会串台；
    * 改法为空串时不产 added 段（那是"删掉这个词"，不是"换成空白"）。
    """

    kind: Literal["plain", "removed", "added"]
    text: str = ""
    index: int | None = None
    detection_id: str | None = None


class _Span(NamedTuple):
    """一处已确认可安全套回原文的改动。

    区间在这里**收窄成非空的 int**——"能不能用"是构造 `_Span` 时一次性判定的事实，
    之后的分段与替换都不必再各自 assert 一遍（那等于把契约复述在每个调用方里）。
    """

    detection: Detection
    start: int
    end: int


__all__ = [
    "Decision",
    "Mark",
    "apply_decisions",
    "finalize_text",
    "build_marked_segments",
    "build_result_text",
]


def apply_decisions(
    detections: list[Detection],
    decisions: dict[str, Decision],
    overrides: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> list[Detection]:
    """按使用者的表态算出定稿条目：**只含 accepted 与 edited**。

    - ``edited``：用 ``overrides[id]`` 顶替 ``corrected``（人以自己的改法为准）；
      没给 override 时保留 AI 原改法。
    - ``rejected`` / ``cancelled``：剔除。
    - **没表态的、id 缺失的：一律剔除（保守）**——宁可少改，也不能把使用者没点头的
      改动算进定稿。
    - **C-2 防御**：``recheck.status == "withdrawn"`` 的条目一律剔除——AI 自己都已经
      承认判断有误，即使界面还留着旧的「采纳」也不该生效。

    ``warnings`` 是可选的出参容器：需要知道「剔了几条」的调用方传一个 list 进来。
    """
    overrides = overrides or {}
    kept: list[Detection] = []
    withdrawn = 0

    for det in detections:
        if not det.id:
            continue  # 无从引用的条目：保守剔除
        if det.recheck is not None and det.recheck.status == "withdrawn":
            withdrawn += 1
            continue
        verdict = decisions.get(det.id)
        if verdict not in _KEPT:
            continue

        if verdict == "edited":
            new_corrected = overrides.get(det.id)
            if new_corrected:
                det = det.model_copy(update={"corrected": new_corrected})
        kept.append(det)

    if withdrawn and warnings is not None:
        warnings.append(f"有 {withdrawn} 条经复核已撤销，未计入批改结果")

    return kept


def finalize_text(text: str, kept: list[Detection]) -> tuple[str, list[str]]:
    """把定稿条目按字符区间套回原文，返回 ``(改后全文, 提示列表)``。

    区间是相对**原始输入文本**的 ``[start, end)``。按区间**逆序**替换，前面的替换
    才不会把后面的位置推歪。

    不自动替换（只留在讲解里并记一条提示）的情况：
    * ``aligned=False`` 或 start/end 缺失——当初就没定位到，硬套会改错地方；
    * 区间越界或空区间——数据不合法；
    * 与前一区间重叠——两条改动叠在同一处，自动套用结果不可控。

    这套筛选由 ``_split_usable()`` 单点定义，批阅稿（``build_marked_segments``）
    与清单（``build_result_text``）走同一套规则——三条产物对"哪些改动真的生效了"
    必须口径一致，否则编号会串台。
    """
    warnings: list[str] = []
    spans, _ = _split_usable(text, kept, warnings)

    result = text
    for sp in reversed(spans):  # 逆序替换，前面的改动才不会把后面的位置推歪
        result = result[: sp.start] + sp.detection.corrected + result[sp.end :]

    return result, warnings


def build_marked_segments(text: str, kept: list[Detection]) -> tuple[list[Mark], list[str]]:
    """把定稿条目**印回原文**，返回 ``(分段序列, 提示列表)``。

    这是清单做不到、而我们有字符位置才做得到的事：使用者看到的不是另一份
    "原文 → 改法"列表，而是**一篇挂着批改痕迹的作文**（划掉 + 顶改法 + 编号）。

    * 划掉的永远是**原文切片** ``text[start:end]``，不是 ``detection.original``
      —— 两者对不上时以原文为准，否则渲染出来的句子会不通；
    * 编号按**原文位置**从 1 起，与 ``build_result_text`` 的清单编号一致；
    * 套不上的条目（定位不到 / 越界 / 重叠）不进分段，但会进 ``warnings``。
      **调用方必须把 warnings 显示出来**——否则使用者拿到一篇看着改好了的全文，
      却不知道有几处根本没套上去。
    """
    warnings: list[str] = []
    spans, _ = _split_usable(text, kept, warnings)

    marks: list[Mark] = []
    cursor = 0
    for i, sp in enumerate(spans, start=1):
        if sp.start > cursor:
            marks.append(Mark(kind="plain", text=text[cursor : sp.start]))
        marks.append(Mark(kind="removed", text=text[sp.start : sp.end]))
        if sp.detection.corrected:  # 改法为空 = 删掉这个词，不该顶一个空段上去
            marks.append(
                Mark(
                    kind="added",
                    text=sp.detection.corrected,
                    index=i,
                    detection_id=sp.detection.id,
                )
            )
        cursor = sp.end
    if cursor < len(text):
        marks.append(Mark(kind="plain", text=text[cursor:]))

    return marks, warnings


def build_result_text(
    text: str,
    kept: list[Detection],
    comment: str = "",
    edited_ids: set[str] | None = None,
) -> str:
    """把定稿条目拼成一份**可直接交出去的清单**文本。

    清单模板由本函数**单点定义**：装配层（`web/server.py`）接线之后，前端的预览与
    复制都应改为取本函数的输出，不再各拼一份（那两份迟早会长得不一样）。
    ⚠️ **当前前端仍在自己拼，接线是下一步的事**——本函数不假设调用方已经存在。

    * 顺序是**能印回原文的按位置在前，印不上的排最后**。这条不是排版偏好：
      清单的 1、2、3 必须与批阅稿的上标编号指同一条，把印不上的条目混在中间
      就会串台（使用者对着批阅稿看讲解会看错行）；
    * ``edited_ids`` 里的条目标「（手动修改）」，看得出哪条是人工手改的；
    * 印不上的条目**仍留在清单里**（讲解还有用），只是印不回原文；
    * ``comment`` 是使用者自己写的整体评语，原样附在末尾，**不经过模型**。
    """
    edited = edited_ids or set()
    # 只借顺序：warning 由 finalize_text / build_marked_segments 负责报给界面
    spans, dropped = _split_usable(text, kept, [])

    lines = ["【批改结果】"]
    if not kept:
        lines.append("本轮没有保留任何修改。")
    ordered = [sp.detection for sp in spans] + dropped
    for i, det in enumerate(ordered, start=1):
        marker = "（手动修改）" if det.id in edited else ""
        lines.append(f"{i}. {det.original} → {det.corrected}{marker}")
        if det.explanation_zh:
            lines.append(f"   讲解：{det.explanation_zh}")

    out = "\n".join(lines)
    note = comment.strip()
    if note:
        out += f"\n\n整体评语：{note}"
    return out


def _split_usable(
    text: str, kept: list[Detection], warnings: list[str]
) -> tuple[list[_Span], list[Detection]]:
    """一次算清两件事：**能安全套用的区间**（按位置升序）+ **印不上的条目**。

    改后全文、批阅稿、清单三者共用这一套判定——"哪些改动真的生效了"只能有一份
    答案。尤其编号：清单的前 m 条必须与批阅稿的上标 1..m 指同一条，
    否则同一个数字在两处指到不同的错误上。

    ``warnings`` 是出参容器（与 ``apply_decisions`` 用法一致）；只想借顺序、
    不需要提示的调用方传一个空 list 即可。
    """
    usable: list[Detection] = []
    dropped: list[Detection] = []
    for det in kept:
        label = det.id or "?"
        if not det.aligned or det.start is None or det.end is None:
            warnings.append(f"{label}：未能定位到原文位置，未自动套用改动（讲解仍保留）")
            dropped.append(det)
            continue
        if det.start < 0 or det.end > len(text) or det.start >= det.end:
            warnings.append(f"{label}：区间不合法（{det.start}-{det.end}），未自动套用改动")
            dropped.append(det)
            continue
        usable.append(det)

    # 按起点升序扫一遍丢掉重叠（只与上一条比对即可）
    ordered = sorted(usable, key=lambda d: (d.start or 0, d.end or 0))
    spans: list[_Span] = []
    prev_end = -1
    for det in ordered:
        assert det.start is not None and det.end is not None  # 上面已过滤
        if det.start < prev_end:
            warnings.append(f"{det.id or '?'}：与前一条改动的区间重叠，未自动套用（请手动处理）")
            dropped.append(det)
            continue
        spans.append(_Span(det, det.start, det.end))
        prev_end = det.end
    return spans, dropped
