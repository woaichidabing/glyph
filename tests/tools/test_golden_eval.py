"""金标评测算法离线单测：normalize、双向子串匹配、TP/FP/FN、P/R/F0.5、OK/SOFT 尺度。

不联网，喂构造的 detections 验证打分逻辑本身（真实模型跑金标在 integration 测试）。
"""

from __future__ import annotations

import pytest
from golden_eval import aggregate, f_beta, is_match, normalize, score_case


def _det(original: str, tier: str = "error") -> dict:
    return {"original": original, "corrected": "x", "tier": tier, "type": "hard"}


def test_normalize_lowercases_and_strips_non_alnum():
    assert normalize("Don't like, So I Eat.") == "dontlikesoieat"
    assert normalize("I  is ") == "iis"


def test_is_match_is_bidirectional_substring_and_order_insensitive_to_case():
    # AI 标得比 gold 短（取了错误词）也应命中
    assert is_match("go to school", "go") is True
    # AI 标得比 gold 长（取了整句）也命中
    assert is_match("go", "I go to school") is True
    assert is_match("don't like", "Don't Like") is True
    # 完全不相干不命中
    assert is_match("recieve", "student") is False


def test_hard_case_all_hit():
    case = {"id": "c", "bucket": "hard", "must_catch": ["I is", "very like"]}
    score = score_case(case, [_det("I is"), _det("very like")])
    assert (score["tp"], score["fp"], score["fn"]) == (2, 0, 0)


def test_hard_case_missed_is_fn():
    case = {"id": "c", "bucket": "hard", "must_catch": ["I is", "very like"]}
    score = score_case(case, [_det("I is")])
    assert (score["tp"], score["fp"], score["fn"]) == (1, 0, 1)
    assert score["unmatched_golds"] == ["very like"]


def test_extra_detection_not_matching_gold_is_fp():
    case = {"id": "c", "bucket": "hard", "must_catch": ["I is"]}
    score = score_case(case, [_det("I is"), _det("student")])
    assert (score["tp"], score["fp"], score["fn"]) == (1, 1, 0)


def test_extra_suggestion_in_error_case_is_not_counted_as_fp():
    case = {"id": "c", "bucket": "chinglish", "must_catch": ["open the light"]}
    # 命中 gold 的一条 + 一条额外的"可改可不改"建议
    score = score_case(case, [_det("open the light"), _det("maybe", tier="suggestion")])
    assert (score["tp"], score["fp"], score["fn"]) == (1, 0, 0)
    assert len(score["extra_suggestions"]) == 1
    # 额外的硬 error 仍算误报
    score2 = score_case(case, [_det("open the light"), _det("maybe", tier="error")])
    assert score2["fp"] == 1 and score2["extra_suggestions"] == []


def test_one_detection_cannot_cover_two_overlapping_golds():
    # 两个 gold 只有一条 AI 结果：只算一个 TP，另一个算 FN，不重复计数
    case = {"id": "c", "bucket": "hard", "must_catch": ["like", "like playing"]}
    score = score_case(case, [_det("like playing")])
    assert (score["tp"], score["fp"], score["fn"]) == (1, 0, 1)


def test_ok_case_any_detection_is_false_positive():
    case = {"id": "ok", "bucket": "ok", "must_catch": []}
    assert score_case(case, [])["fp"] == 0
    scored = score_case(case, [_det("see", tier="suggestion"), _det("that")])
    assert scored["fp"] == 2


def test_soft_case_suggestion_tolerated_but_error_is_over_correction():
    case = {"id": "soft", "bucket": "soft", "must_catch": []}
    assert score_case(case, [_det("x", tier="suggestion")])["fp"] == 0
    assert score_case(case, [_det("x", tier="error")])["fp"] == 1


def test_fbeta_weights_precision_more():
    # P=0.8 R=2/3：F0.5 应偏向精确率
    p, r = 0.8, 2 / 3
    assert f_beta(p, r, beta=0.5) == pytest.approx(0.7692, abs=1e-3)


def test_aggregate_counts_and_per_bucket_recall():
    scores = [
        score_case(
            {"id": "h1", "bucket": "hard", "must_catch": ["a", "b"]}, [_det("a"), _det("b")]
        ),  # tp2
        score_case({"id": "h2", "bucket": "hard", "must_catch": ["c"]}, []),  # fn1
        score_case(
            {"id": "x1", "bucket": "chinglish", "must_catch": ["d"]}, [_det("d"), _det("extra")]
        ),  # tp1 fp1
        score_case({"id": "ok1", "bucket": "ok", "must_catch": []}, [_det("see")]),  # fp1（误报句）
        score_case({"id": "ok2", "bucket": "ok", "must_catch": []}, []),
        score_case(
            {"id": "s1", "bucket": "soft", "must_catch": []}, [_det("x", tier="error")]
        ),  # fp1（过度句）
    ]
    agg = aggregate(scores)
    assert (agg["tp"], agg["fp"], agg["fn"]) == (3, 3, 1)
    assert agg["bucket_recall"]["hard"] == pytest.approx(2 / 3)
    assert agg["bucket_recall"]["chinglish"] == 1.0
    assert agg["ok_false_positive_cases"] == ["ok1"]
    assert agg["soft_over_error_cases"] == ["s1"]
