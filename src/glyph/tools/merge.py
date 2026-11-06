"""批改结果的合并与清洗（纯函数、离线、确定性）。

把"本地拼写库"和"批改模型"两路检测合并成可直接渲染的结果：

- :func:`align_detections`：以 ``original`` 片段为锚点在原文重定位（不信模型
  给的 token 偏移）；
- 后续切片负责两路合并 / 去重叠 / 建议限量 / 编号 / 统计（确定错误不限量）。
"""

from __future__ import annotations

from .grading_models import Detection

__all__ = [
    "align_detections",
    "merge_detections",
    "cap_detections",
    "assign_ids",
    "count_stats",
    "finalize_detections",
]

# suggestion 软顶：可改可不改的主观润色再多也只展示这么多条，其余截断并提示。
# 注意：确定错误(error)不设任何数量上限——它是客观对错，错得多就如实多报，
# 绝不能为了"画面别太满"把硬错误降级或丢弃；真要控制视觉密度应由展示层折叠。
SUGGESTION_SOFT_CAP = 20

# 区间冲突时的保留优先级（元组越小越该留）：
# 1) tier：确定错误(error) 压过 可改可不改(suggestion)；
# 2) source：同档时模型语义判断压过本地拼写库（拼写库可能误报专名）。
_TIER_RANK = {"error": 0, "suggestion": 1}
_SOURCE_RANK = {"model": 0, "spell": 1}


def _priority(d: Detection) -> tuple[int, int]:
    return (_TIER_RANK.get(d.tier, 1), _SOURCE_RANK.get(d.source, 0))


def _find_all(text: str, needle: str) -> list[int]:
    """返回 needle 在 text 中所有出现的起始下标（大小写敏感）。"""
    positions: list[int] = []
    if not needle:
        return positions
    idx = text.find(needle)
    while idx != -1:
        positions.append(idx)
        idx = text.find(needle, idx + 1)
    return positions


def _locate(essay: str, original: str, hint: int | None) -> int | None:
    """在原文中定位 original，返回起始下标；找不到返回 None。

    先精确匹配；精确不到再做大小写不敏感匹配。模型给的 hint 仅用于同一短语
    多次出现时挑最近的一次。
    """
    candidates = _find_all(essay, original)
    if not candidates:
        lowered_haystack, lowered_needle = essay.lower(), original.lower()
        candidates = _find_all(lowered_haystack, lowered_needle)
    if not candidates:
        return None
    origin_hint = hint if isinstance(hint, int) else 0
    return min(candidates, key=lambda pos: abs(pos - origin_hint))


def align_detections(detections: list[Detection], essay: str) -> list[Detection]:
    """强制每条检测的 start/end/original 与原文一致。

    - 已有区间且 ``essay[start:end] == original``：原样保留（本地拼写器走这条）；
    - 否则用 original 全文搜索重定位，模型给的 start 只作多次出现时的提示；
    - 大小写不敏感命中时，把 original 校正为原文真实写法；
    - 定位不到：start/end 置 None、aligned=False，条目保留不丢。
    """
    out: list[Detection] = []
    essay_len = len(essay)

    for d in detections:
        d = d.model_copy()
        original = d.original

        if not isinstance(original, str) or not original:
            d.aligned = False
            d.start, d.end = None, None
            out.append(d)
            continue

        if (
            isinstance(d.start, int)
            and isinstance(d.end, int)
            and 0 <= d.start < d.end <= essay_len
            and essay[d.start : d.end] == original
        ):
            d.aligned = True
            out.append(d)
            continue

        pos = _locate(essay, original, d.start)
        if pos is not None:
            d.start = pos
            d.end = pos + len(original)
            d.original = essay[pos : pos + len(original)]
            d.aligned = True
        else:
            d.start, d.end = None, None
            d.aligned = False
        out.append(d)

    return out


def _drop_overlaps(detections: list[Detection]) -> list[Detection]:
    """同一段文字只留一条：高优先级压过低优先级。

    按起点排序后贪心扫描；后到者与已保留区间重叠时，只有严格更高优先级才
    替换（确定错误替换建议、模型替换拼写），否则丢弃。未对齐（无区间）的
    条目不参与区间竞争，原样附在最后。
    """
    located = [d for d in detections if d.aligned and d.start is not None]
    unaligned = [d for d in detections if not (d.aligned and d.start is not None)]

    located.sort(key=lambda d: (d.start, _priority(d), (d.end or 0) - d.start))  # type: ignore[operator]

    kept: list[Detection] = []
    for d in located:
        overlapping = [
            k for k in kept if (d.start or 0) < (k.end or 0) and (k.start or 0) < (d.end or 0)
        ]
        if not overlapping:
            kept.append(d)
            continue
        strongest = min(overlapping, key=_priority)
        if _priority(d) < _priority(strongest):
            # 后到的 d 严格更强：替换整簇，成为赢家
            winner = d
            if winner.error_code is None:
                # 赢家没认出知识点，从被它挤掉的条目继承标签（D19）
                winner = winner.model_copy(
                    update={
                        "error_code": next(
                            (k.error_code for k in overlapping if k.error_code), None
                        )
                    }
                )
            for k in overlapping:
                kept.remove(k)
            kept.append(winner)
        elif strongest.error_code is None and d.error_code:
            # d 被丢弃，但已在列的赢家没认出知识点：继承被挤掉条目的标签（D19）。
            # 典型：模型(硬错)与本地拼写同区间、模型胜出却没标 code，补上 SPELL 防去重丢标签。
            idx = next(i for i, k in enumerate(kept) if k is strongest)
            kept[idx] = strongest.model_copy(update={"error_code": d.error_code})
        # 否则 d 丢弃，避免同一段被重复标注

    kept.sort(key=lambda d: d.start)  # type: ignore[arg-type]
    return kept + unaligned


def merge_detections(
    model_detections: list[Detection],
    spell_detections: list[Detection],
    essay: str,
) -> list[Detection]:
    """合并模型与本地拼写两路检测：先对齐原文，再去重叠。

    不在此处编号 / 限条数（后续切片负责）。不修改传入对象。
    """
    aligned = align_detections([*model_detections, *spell_detections], essay)
    return _drop_overlaps(aligned)


def cap_detections(
    detections: list[Detection],
    *,
    suggestion_soft_cap: int = SUGGESTION_SOFT_CAP,
) -> tuple[list[Detection], list[str]]:
    """限制展示量，但**绝不为控数量改动客观对错分级**。

    - 确定错误(error)：全部保留，不降级、不截断。硬错误是客观对错，漏报或把它
      改成"建议"都会误导学习者——错得越多越该如实告知，篇幅长短不再是降级理由；
      若一次实在太多需要折叠，应由展示层分页/折叠处理，数据层不隐瞒。
    - 建议(suggestion)：主观润色、边际价值递减，超过软顶的截断，并通过 warning
      明确告知"有 N 条建议未展示"，不丢得无声无息。
    """
    truncated = 0
    final: list[Detection] = []
    suggestion_kept = 0
    for d in detections:
        d = d.model_copy()
        if d.tier == "suggestion":
            if suggestion_kept >= suggestion_soft_cap:
                truncated += 1
                continue
            suggestion_kept += 1
        final.append(d)

    warnings: list[str] = []
    if truncated:
        warnings.append(f"有 {truncated} 条建议因数量较多未展示")
    return final, warnings


def assign_ids(detections: list[Detection]) -> list[Detection]:
    """按最终展示顺序分配稳定编号 d1、d2…（供以后单条驳回引用）。"""
    return [d.model_copy(update={"id": f"d{i}"}) for i, d in enumerate(detections, start=1)]


def count_stats(detections: list[Detection], essay: str) -> dict[str, int]:
    """统计字数与各档条数（按限条、降级后的最终 tier 计数）。"""
    error_count = sum(1 for d in detections if d.tier == "error")
    suggestion_count = sum(1 for d in detections if d.tier == "suggestion")
    return {
        "words": len(essay.split()),
        "error_count": error_count,
        "suggestion_count": suggestion_count,
        "total": len(detections),
    }


def finalize_detections(
    model_detections: list[Detection],
    spell_detections: list[Detection],
    essay: str,
) -> tuple[list[Detection], dict[str, int], list[str]]:
    """纯本地清洗总装：合并去重 → 建议限量 → 编号 → 统计。

    返回 (detections, stats, warnings)；模型总评 summary 由调用方另填。
    确定错误不做数量限制（见 :func:`cap_detections`）。
    """
    merged = merge_detections(model_detections, spell_detections, essay)
    capped, warnings = cap_detections(merged)
    numbered = assign_ids(capped)
    stats = count_stats(numbered, essay)
    return numbered, stats, warnings
