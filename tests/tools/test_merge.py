"""批改结果合并器的纯函数测试（全程离线，不联网）。

切片 1：原文锚点对齐 ``align_detections``。

背景（旧版踩坑）：模型给的 start/end 是 **token 偏移**，不是 Python 字符索引，
还会单调漂移，不能直接用来原位标红。位置一律以模型返回的 ``original`` 片段为
锚点，在原文里全文重定位；模型给的 start 仅用于"同一片段出现多次时取最近"。
"""

from glyph.tools.grading_models import Detection
from glyph.tools.merge import (
    align_detections,
    assign_ids,
    cap_detections,
    count_stats,
    finalize_detections,
    merge_detections,
)


def det(**kwargs) -> Detection:
    """构造一条检测，默认值补齐必填字段。"""
    kwargs.setdefault("original", "x")
    kwargs.setdefault("corrected", "x")
    kwargs.setdefault("type", "hard")
    kwargs.setdefault("tier", "error")
    return Detection(**kwargs)


def test_align_keeps_correct_span():
    essay = "I very like apples."
    start = essay.index("very like")
    d = det(original="very like", start=start, end=start + len("very like"))

    [out] = align_detections([d], essay)

    assert out.aligned is True
    assert essay[out.start : out.end] == "very like"


def test_align_repositions_token_offset_drift_by_full_text_search():
    # 模型给的 start 是错的（token 偏移漂移），但 original 片段能在原文找到，
    # 必须靠全文搜索纠正，而不是信任它给的数字。
    essay = "I is a student. I very like apples."
    d = det(original="very like", start=3, end=12)

    [out] = align_detections([d], essay)

    assert out.aligned is True
    assert out.start == essay.index("very like")
    assert essay[out.start : out.end] == "very like"


def test_align_picks_occurrence_closest_to_hint_for_repeated_span():
    essay = "I am here. I am there."
    first_i = essay.index("I")
    second_i = essay.index("I", first_i + 1)
    # 提示位置靠近第二个 I，应选第二个出现处
    d = det(original="I", start=second_i - 1, end=second_i)

    [out] = align_detections([d], essay)

    assert out.aligned is True
    assert out.start == second_i


def test_align_is_case_insensitive_and_sets_original_to_actual_text():
    # 模型把句首 Because 写成小写 because；应大小写不敏感定位到原文真实片段，
    # 并把 original 校正成原文里的写法（保证高亮与原文严格一致）。
    essay = "Because it is delicious so I eat it."
    d = det(original="because", start=0, end=7)

    [out] = align_detections([d], essay)

    assert out.aligned is True
    assert essay[out.start : out.end] == "Because"
    assert out.original == "Because"


def test_align_unmatched_span_is_kept_but_marked_unaligned():
    essay = "I am happy."
    d = det(original="zzznotfound", corrected="x", start=2, end=12)

    [out] = align_detections([d], essay)

    assert out.aligned is False
    assert out.start is None
    assert out.end is None
    # 定位不到也不丢条目（前端可放进"其他建议"）
    assert out.original == "zzznotfound"


def test_align_accepts_spellchecker_exact_span():
    essay = "I am a studdent."
    start = essay.index("studdent")
    d = det(
        original="studdent",
        corrected="student",
        source="spell",
        start=start,
        end=start + len("studdent"),
    )

    [out] = align_detections([d], essay)

    assert out.aligned is True
    assert out.start == start


def test_align_empty_original_does_not_crash():
    essay = "I am happy."
    d = det(original="", corrected="x")

    [out] = align_detections([d], essay)

    assert out.aligned is False
    assert out.start is None


# ---------- 切片 2：两路合并 + 区间去重叠 ----------


def test_merge_keeps_distinct_findings_sorted_by_position():
    essay = "I is a studdent. I very like it."
    model = [
        det(original="I is", corrected="I am", type="hard", tier="error"),
        det(original="very like", corrected="really like", type="chinglish", tier="suggestion"),
    ]
    spell = [det(original="studdent", corrected="student", source="spell")]

    out = merge_detections(model, spell, essay)

    assert [d.original for d in out] == ["I is", "studdent", "very like"]
    assert all(d.aligned for d in out)


def test_merge_dedupes_same_span_preferring_model_over_spell():
    essay = "I am a studdent."
    model = [det(original="studdent", corrected="student", source="model")]
    spell = [det(original="studdent", corrected="student", source="spell")]

    out = merge_detections(model, spell, essay)

    assert len(out) == 1
    assert out[0].source == "model"


def test_merge_model_without_code_inherits_spell_code_on_same_span():
    # 模型（硬错）压过本地拼写却没认出知识点时，把拼写的 SPELL 标签带上，防去重丢标签（D19）
    essay = "I am a studdent."
    model = [det(original="studdent", corrected="student", source="model")]
    spell = [det(original="studdent", corrected="student", source="spell", error_code="SPELL")]

    out = merge_detections(model, spell, essay)

    assert len(out) == 1
    assert out[0].source == "model"
    assert out[0].error_code == "SPELL"


def test_merge_model_own_code_wins_over_spell_code():
    essay = "I goes home."
    model = [det(original="goes", corrected="go", source="model", error_code="VERB:TENSE")]
    spell = [det(original="goes", corrected="go", source="spell", error_code="SPELL")]

    out = merge_detections(model, spell, essay)

    assert len(out) == 1
    assert out[0].error_code == "VERB:TENSE"  # 模型语义判断优先于拼写库


def test_merge_same_start_error_outranks_suggestion():
    essay = "I very like apples."
    suggestion = det(
        original="very like apples", corrected="really like apples", type="wordy", tier="suggestion"
    )
    error = det(original="very like", corrected="really like", type="hard", tier="error")

    out = merge_detections([suggestion, error], [], essay)

    assert len(out) == 1
    assert out[0].tier == "error"
    assert out[0].original == "very like"


def test_merge_later_error_replaces_earlier_overlapping_suggestion():
    # 先到的 suggestion 区间更大、起点更早，后到的 error 落在其中：
    # error 更该被看到，应替换掉 suggestion。
    essay = "I very like apples today."
    suggestion = det(
        original="very like apples", corrected="really like apples", type="wordy", tier="suggestion"
    )
    error = det(original="like apples", corrected="really like apples", type="hard", tier="error")

    out = merge_detections([suggestion, error], [], essay)

    assert len(out) == 1
    assert out[0].tier == "error"
    assert out[0].original == "like apples"


def test_merge_keeps_unaligned_findings_last_without_dropping():
    essay = "I am happy."
    model = [
        det(original="am", corrected="are"),
        det(original="zzznotfound", corrected="x"),
    ]

    out = merge_detections(model, [], essay)

    assert len(out) == 2
    assert out[0].original == "am" and out[0].aligned is True
    assert out[1].original == "zzznotfound" and out[1].aligned is False
    assert out[1].start is None


def test_merge_does_not_mutate_inputs():
    essay = "I very like it."
    d = det(original="very like", start=2, end=11)

    merge_detections([d], [], essay)

    assert d.start == 2 and d.end == 11


# ---------- 切片 3：条数上限 / 编号 / 统计 ----------


def _spaced_errors(n: int) -> list[Detection]:
    return [
        det(original=f"w{i}", corrected=f"c{i}", start=i * 10, end=i * 10 + 1) for i in range(n)
    ]


def test_cap_keeps_every_error_no_matter_how_dense():
    # 客观硬错误绝不能因为"篇幅预算"被降级成建议：错得多就如实多报。
    # 旧逻辑按每 12 词 1 处给名额，30 词只允许 3 个 error，第 4 个起被粉饰成
    # suggestion（真实踩坑：26 词作文 4 处时态错被显示成"3 要改 + 1 建议"）。
    capped, warnings = cap_detections(_spaced_errors(5))

    assert [d.tier for d in capped] == ["error"] * 5
    assert len(capped) == 5  # 一条都不能丢、也不能改档
    assert capped[3].start == 30  # 区间信息保留
    assert not any("转为建议" in w or "确定错误" in w for w in warnings)


def test_cap_keeps_errors_beyond_old_hard_cap():
    # 旧的 12 处硬顶也一并取消：哪怕一篇有 15 处确定错误，也全部如实保留。
    capped, _ = cap_detections(_spaced_errors(15))

    assert len(capped) == 15
    assert all(d.tier == "error" for d in capped)


def test_cap_does_not_mutate_input_tiers():
    errors = _spaced_errors(5)

    cap_detections(errors)

    assert all(d.tier == "error" for d in errors)


def test_cap_truncates_suggestions_beyond_soft_cap_with_warning():
    many = [
        Detection(
            original=f"s{i}",
            corrected=f"c{i}",
            type="wordy",
            tier="suggestion",
            start=i * 10,
            end=i * 10 + 1,
        )
        for i in range(25)
    ]

    capped, warnings = cap_detections(many)

    assert len(capped) == 20
    assert all(d.tier == "suggestion" for d in capped)
    assert any("未展示" in w for w in warnings)  # 截断要明确告知


def test_cap_keeps_all_errors_while_only_truncating_suggestions():
    errors = _spaced_errors(15)  # 远超旧硬顶的硬错误：全部保留
    suggestions = [
        Detection(
            original=f"s{i}",
            corrected=f"c{i}",
            type="wordy",
            tier="suggestion",
            start=1000 + i * 10,
            end=1000 + i * 10 + 1,
        )
        for i in range(25)  # 建议超软顶：截断到 20
    ]

    capped, warnings = cap_detections([*errors, *suggestions])

    assert sum(1 for d in capped if d.tier == "error") == 15
    assert sum(1 for d in capped if d.tier == "suggestion") == 20
    assert any("未展示" in w for w in warnings)
    # 只能告知"建议未展示"，绝不能出现把硬错误转成建议的说法
    assert not any("转为建议" in w for w in warnings)


def test_assign_ids_are_sequential_and_stable():
    dets = [det(original="a", start=0, end=1), det(original="b", start=2, end=3)]

    out = assign_ids(dets)

    assert [d.id for d in out] == ["d1", "d2"]


def test_count_stats_counts_by_final_tier():
    dets = [
        det(original="a", tier="error", start=0, end=1),
        det(original="b", tier="error", start=2, end=3),
        Detection(original="c", corrected="c", type="wordy", tier="suggestion", start=4, end=5),
    ]

    stats = count_stats(dets, "one two three four")

    assert stats == {"words": 4, "error_count": 2, "suggestion_count": 1, "total": 3}


def test_finalize_end_to_end_merges_caps_numbers_and_counts():
    essay = "I is a studdent."
    model = [det(original="I is", corrected="I am", type="hard", tier="error")]
    spell = [det(original="studdent", corrected="student", source="spell")]

    dets, stats, warnings = finalize_detections(model, spell, essay)

    assert [d.id for d in dets] == ["d1", "d2"]
    assert [d.original for d in dets] == ["I is", "studdent"]
    assert stats["error_count"] == 2 and stats["total"] == 2 and stats["words"] == 4
    assert warnings == []
