"""金标评测打分（纯函数，离线可测）。

口径复用旧项目 legacy/scripts/eval_compare.py：
- normalize：小写并去掉所有非字母数字字符（撇号、空格、标点都抹平）；
- 命中判定：gold 片段与 AI 标注片段 normalize 后互为子串（双向），容忍 AI 取长取短；
- F0.5：β=0.5，精确率权重高于召回率——批改"别乱改"比"多挑错"更重要。

bucket 语义见 tests/fixtures/golden/cases.json：
- hard/chinglish/wordy：must_catch 命中算 TP，漏算 FN；AI 额外报的 error 算 FP，
  额外的 suggestion（可改可不改的软档）单列 extra_suggestions，不与硬误报同罚；
- soft：允许 suggestion，报 error 才算 FP（过度批改）；
- ok：任何 detection 都算 FP（正确句零误报红线，suggestion 也不例外）。
"""

from __future__ import annotations

import re
from typing import Any

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


def is_match(gold: str, ai_original: str) -> bool:
    g, a = normalize(gold), normalize(ai_original)
    if not g or not a:
        return False
    return g in a or a in g


def score_case(case: dict[str, Any], detections: list[dict[str, Any]]) -> dict[str, Any]:
    bucket = case["bucket"]
    golds = list(case.get("must_catch", []))

    tp = fp = fn = 0
    matched_golds: list[str] = []
    unmatched_golds: list[str] = []
    false_dets: list[dict[str, Any]] = []
    extra_suggestions: list[dict[str, Any]] = []

    if bucket == "ok":
        # 正确句：任何标注都是误报（含 suggestion，零容忍红线）
        false_dets = list(detections)
        fp = len(false_dets)
    elif bucket == "soft":
        # 可接受句：只有 tier=error 才算过度批改，suggestion 容忍
        false_dets = [d for d in detections if d.get("tier") == "error"]
        fp = len(false_dets)
    else:
        used: set[int] = set()
        for gold in golds:
            hit = next(
                (
                    di
                    for di, d in enumerate(detections)
                    if di not in used and is_match(gold, d.get("original", ""))
                ),
                None,
            )
            if hit is not None:
                tp += 1
                used.add(hit)
                matched_golds.append(gold)
            else:
                fn += 1
                unmatched_golds.append(gold)
        for di, d in enumerate(detections):
            if di in used:
                continue
            if d.get("tier") == "suggestion":
                # 可改可不改的软建议不与硬误报同罚，单列观察，防止建议泛滥靠总量约束
                extra_suggestions.append(d)
            else:
                fp += 1
                false_dets.append(d)

    return {
        "id": case["id"],
        "bucket": bucket,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "matched_golds": matched_golds,
        "unmatched_golds": unmatched_golds,
        "false_dets": false_dets,
        "extra_suggestions": extra_suggestions,
    }


def f_beta(precision: float, recall: float, beta: float = 0.5) -> float:
    b2 = beta * beta
    denom = b2 * precision + recall
    if denom == 0:
        return 0.0
    return (1 + b2) * precision * recall / denom


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(s["tp"] for s in scores)
    fp = sum(s["fp"] for s in scores)
    fn = sum(s["fn"] for s in scores)

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else None
    f05 = f_beta(precision, recall) if recall is not None else None

    # 分桶召回（只对"应有错误"的桶）
    bucket_recall: dict[str, float] = {}
    for bucket in ("hard", "chinglish", "wordy"):
        btp = sum(s["tp"] for s in scores if s["bucket"] == bucket)
        bfn = sum(s["fn"] for s in scores if s["bucket"] == bucket)
        if btp + bfn:
            bucket_recall[bucket] = btp / (btp + bfn)

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f0.5": f05,
        "bucket_recall": bucket_recall,
        "extra_suggestions": sum(len(s["extra_suggestions"]) for s in scores),
        "ok_false_positive_cases": [s["id"] for s in scores if s["bucket"] == "ok" and s["fp"] > 0],
        "soft_over_error_cases": [s["id"] for s in scores if s["bucket"] == "soft" and s["fp"] > 0],
        "scores": scores,
    }
