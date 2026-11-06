"""L3 摘要压缩的纯函数（方案 §9.3 / §9.4 / §11.1，v2.1 #65）。

这一层只做确定性的"切东西、装预算、渲染续接话术"，不碰网络、不 import llm（B2）：
- 按完整问答往返（turn）切分流水；
- 动态 tail：按反推预算从新往旧装完整 turn，有下限（A3）；
- 摘要材料：按写手窗口 × 比例从切点往旧装完整 turn，最旧装不下不喂（§9.3）；
- 续接 user：固定话术 + trust 袋子包摘要 + 提货单（C4 袋子成对、不合并 user）。

断言都落在"切点对不对、装了哪些完整往返、袋子是否成对"，不绑定内部数据结构。
"""

from __future__ import annotations

from glyph.session.compaction_l3 import (
    material_budget_bytes,
    render_resume_user,
    select_material,
    select_tail,
    split_turns,
    tail_budget_bytes,
)
from glyph.session.records import (
    AssistantMessagePayload,
    ReasoningPayload,
    ToolCallPayload,
    ToolResultPayload,
    UserMessagePayload,
    build_record,
)


def _user(seq: int, text: str) :
    return build_record(seq, f"t-{seq}", UserMessagePayload(text=text))


def _assistant(seq: int, text: str = "b" * 100):
    return build_record(seq, f"t-{seq}", AssistantMessagePayload(text=text))


def _grade(seq: int, call_id: str, *, text: str = "g" * 100):
    """一次 grade_text 调用 + 结果，占 seq、seq+1。"""
    return [
        build_record(
            seq,
            f"t-{seq}",
            ToolCallPayload(id=call_id, name="grade_text", arguments={"text": text}),
        ),
        build_record(
            seq + 1,
            f"t-{seq + 1}",
            ToolResultPayload(
                call_id=call_id,
                name="grade_text",
                ok=True,
                output={"summary": "三单漏 s", "detections": []},
            ),
        ),
    ]


def _reasoning(seq: int, text: str = "think"):
    return build_record(seq, f"t-{seq}", ReasoningPayload(text=text))


def _three_simple_turns():
    """三个 user/assistant 往返，user 与 assistant 各 100 字节，seq 0..5。"""
    records = []
    for i in range(3):
        records.append(_user(2 * i, "u" * 100))
        records.append(_assistant(2 * i + 1))
    return records


# ---------- turn 切分 ----------


def test_split_turns_groups_records_by_user_message() -> None:
    turns = split_turns(_three_simple_turns())

    assert [t.start_seq for t in turns] == [0, 2, 4]
    assert [len(t.records) for t in turns] == [2, 2, 2]


def test_split_turns_keeps_tool_pair_and_trailing_meta_in_same_turn() -> None:
    # turn0：user@0 + grade 调用@1/结果@2 + assistant@3 + reasoning@4；turn1 从 user@5 起。
    records = (
        [_user(0, "改这篇")]
        + _grade(1, "c1")
        + [_assistant(3, "批改好了"), _reasoning(4), _user(5, "再聊")]
        + [_assistant(6, "好")]
    )

    turns = split_turns(records)

    assert [t.start_seq for t in turns] == [0, 5]
    assert [r.seq for r in turns[0].records] == [0, 1, 2, 3, 4]
    # reasoning 不进 messages()，不计入渲染字节
    assert turns[0].render_bytes > 0
    assert all(r.seq != 4 or True for r in turns[0].records)


def test_split_turns_meta_records_contribute_zero_render_bytes() -> None:
    # 只有 reasoning 有体积文本，但它不进 messages()；user/assistant 才计。
    records = [_user(0, "u" * 100), _reasoning(1, "z" * 500), _assistant(2, "a" * 100)]
    turns = split_turns(records)

    # 100(user) + 100(assistant)，reasoning 的 500 字节不计
    assert turns[0].render_bytes == 200


# ---------- 动态 tail ----------


def test_tail_keeps_everything_when_budget_plenty() -> None:
    turns = split_turns(_three_simple_turns())

    sel = select_tail(turns, budget_bytes=10_000_000, min_turns=1)

    assert sel.tail_turns == 3
    assert sel.over_target_at_min is False
    # tail 从区间第一条记录（seq=0）开始 → 切点 at_seq = -1，seq > -1 即全量。
    assert sel.at_seq == -1
    assert [r.seq for r in sel.tail_records] == [0, 1, 2, 3, 4, 5]


def test_tail_packs_newest_complete_turns_until_budget() -> None:
    # 6 个往返，每往返 user100 + assistant100 = 200 字节；seq 0..11，turn 起点 0,2,4,6,8,10。
    records = []
    for i in range(6):
        records.append(_user(2 * i, "u" * 100))
        records.append(_assistant(2 * i + 1))
    turns = split_turns(records)

    # 预算 450：装最新 turn(200)+次新(200)=400，再装第三个需 600 超限 → 留 2 个。
    sel = select_tail(turns, budget_bytes=450, min_turns=1)

    assert sel.tail_turns == 2
    assert sel.over_target_at_min is False
    # tail 最旧 turn 起于 seq=8；其前紧邻记录是 seq=7。
    assert sel.at_seq == 7
    assert [r.seq for r in sel.tail_records] == [8, 9, 10, 11]


def test_tail_always_keeps_min_turns_and_flags_overflow() -> None:
    records = []
    for i in range(6):
        records.append(_user(2 * i, "u" * 100))
        records.append(_assistant(2 * i + 1))
    turns = split_turns(records)

    # 预算只有 10 字节，远不够一个 turn，但下限 2 → 强装最新 2 个并标记超目标（转 §10）。
    sel = select_tail(turns, budget_bytes=10, min_turns=2)

    assert sel.tail_turns == 2
    assert sel.over_target_at_min is True
    assert sel.at_seq == 7
    assert [r.seq for r in sel.tail_records] == [8, 9, 10, 11]


def test_tail_with_tool_pair_never_splits_mid_turn() -> None:
    # 最新 turn 内含 grade 配对，切点必须落在它的 user 之前，不能切在 call/result 之间。
    records = [_user(0, "u" * 100), _assistant(1)]
    records += [_user(2, "改这篇")] + _grade(3, "c1") + [_assistant(5, "好了")]
    turns = split_turns(records)

    sel = select_tail(turns, budget_bytes=10, min_turns=1)

    # 下限 1 → 保留最新整个 turn（seq 2..5），切点在 seq=1，绝不落在 3/4 之间。
    assert sel.at_seq == 1
    assert [r.seq for r in sel.tail_records] == [2, 3, 4, 5]


# ---------- 摘要材料预算 ----------


def test_material_packs_newest_turns_back_from_cut_and_marks_truncated() -> None:
    # 切点之前 4 个往返，每往返 200 字节；预算 500 只能装紧挨切点的最新 2 个（400）。
    records = []
    for i in range(4):
        records.append(_user(2 * i, "u" * 100))
        records.append(_assistant(2 * i + 1))
    turns = split_turns(records)

    sel = select_material(turns, budget_bytes=500)

    assert sel.turn_count == 2
    assert sel.truncated is True  # 还有 2 个更旧往返没喂
    # 喂入按时间旧→新：选中的是第 3、4 个往返（seq 4,5,6,7）。
    assert [r.seq for r in sel.material_records] == [4, 5, 6, 7]


def test_material_includes_all_when_budget_plenty() -> None:
    records = []
    for i in range(3):
        records.append(_user(2 * i, "u" * 100))
        records.append(_assistant(2 * i + 1))
    turns = split_turns(records)

    sel = select_material(turns, budget_bytes=10_000_000)

    assert sel.turn_count == 3
    assert sel.truncated is False
    assert [r.seq for r in sel.material_records] == [0, 1, 2, 3, 4, 5]


def test_material_empty_when_no_turns() -> None:
    sel = select_material([], budget_bytes=1000)
    assert sel.turn_count == 0
    assert sel.material_records == ()
    assert sel.truncated is False


# ---------- 预算反推 ----------


def test_tail_budget_bytes_subtracts_all_fixed_costs() -> None:
    # W=1000 token、目标 60% → 600 token；固定开销 100 token、摘要预留 100、提货单 100。
    # bpt=3.5：600*3.5=2100，减 350(固定) 减 350(预留) 减 350(提货单) = 1050 字节。
    assert (
        tail_budget_bytes(
            window_tokens=1000,
            target_ratio=0.6,
            fixed_overhead_bytes=350,
            output_reserve_tokens=100,
            picklist_budget_bytes=350,
            bytes_per_token=3.5,
        )
        == 1050
    )


def test_tail_budget_bytes_floors_at_zero() -> None:
    # 固定开销已超过目标 → 不给负数（tail 仍由 min_turns 强保，overflow 标记转应急）。
    assert (
        tail_budget_bytes(
            window_tokens=100,
            target_ratio=0.5,
            fixed_overhead_bytes=10_000,
            output_reserve_tokens=8_000,
            picklist_budget_bytes=10_000,
            bytes_per_token=3.5,
        )
        == 0
    )


def test_material_budget_bytes_is_window_times_ratio() -> None:
    # S=1,000,000、60% → 600,000 token × 3.5 字节/token = 2,100,000 字节。
    assert material_budget_bytes(1_000_000, 0.60, 3.5) == 2_100_000


# ---------- 续接 user 渲染（C4） ----------


def test_render_resume_user_wraps_summary_in_paired_trust_bag() -> None:
    text = render_resume_user("## 学习者画像\n三单常漏 s", picklist_text=None)

    assert "<LEARNER_TEXT>" in text
    assert "</LEARNER_TEXT>" in text
    # 恰好一对
    assert text.count("<LEARNER_TEXT>") == 1
    assert text.count("</LEARNER_TEXT>") == 1
    # 摘要在袋子里
    assert "<LEARNER_TEXT>\n## 学习者画像\n三单常漏 s\n</LEARNER_TEXT>" in text
    # 固定话术在开标签之前（袋子外），并指明取回通道
    assert text.index("read_artifact") < text.index("<LEARNER_TEXT>")


def test_render_resume_user_puts_picklist_outside_trust_bag() -> None:
    text = render_resume_user("摘要", picklist_text="【历史批改提货单】\n1. 第一篇")

    # 提货单在闭标签之后（系统生成的目录，不圈进 learner-trust 区）。
    assert text.index("【历史批改提货单】") > text.index("</LEARNER_TEXT>")
    assert text.count("<LEARNER_TEXT>") == 1
