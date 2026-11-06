"""驳回 → 复核 → 老师再拍板 → 定稿 的**离线整链路**（假 Rechecker，不联网）。

设计文档 §1.12 点名要的两条防线在这里：

1. **复核前后其余条目的 id 与顺序完全不变** —— 复核结果绝不再流经 ``merge.py``，
   否则 ``assign_ids`` 会重排 ``d1…dn``，老师刚驳回的卡片会凭空错位；
2. **三条路径的定稿口径** —— 撤销不进定稿 / 维持按原改法 / 改判按新改法。
"""

from __future__ import annotations

from typing import Any

from glyph.runtime.errors import ToolError
from glyph.tools.grading_models import Detection
from glyph.tools.recheck import RecheckDetectionTool, RecheckVerdict
from glyph.tools.review import apply_decisions, finalize_text

TEXT = "Last weekend was happy. I go to park with my mum and my dad. We was very tired."

FINAL_ALL_ACCEPTED = (
    "Last weekend was very happy. I went to the park with my mum and my dad. We were very tired."
)


def span(sub: str, from_index: int = 0) -> tuple[int, int]:
    start = TEXT.index(sub, from_index)
    return start, start + len(sub)


def sample_detections() -> list[Detection]:
    """run 2 产出的三条（d1 开心→very happy、d2 go→went、d3 was→were）。"""
    s1, e1 = span("happy")
    s2, e2 = span("go to")
    s3, e3 = span("was", 40)  # 第二处 was（We was）
    return [
        Detection(
            id="d1",
            original="happy",
            corrected="very happy",
            type="wordy",
            tier="suggestion",
            explanation_zh="可以更具体",
            start=s1,
            end=e1,
            aligned=True,
        ),
        Detection(
            id="d2",
            original="go to",
            corrected="went to",
            type="hard",
            tier="error",
            explanation_zh="过去的事要用过去式",
            start=s2,
            end=e2,
            aligned=True,
        ),
        Detection(
            id="d3",
            original="was",
            corrected="were",
            type="hard",
            tier="error",
            explanation_zh="we 后面用 were",
            start=s3,
            end=e3,
            aligned=True,
        ),
    ]


class FakeRechecker:
    def __init__(self, verdict: RecheckVerdict) -> None:
        self.verdict = verdict

    async def review(self, req: Any) -> RecheckVerdict:
        return self.verdict


async def recheck(det: Detection, verdict: RecheckVerdict, round_: int = 1) -> dict:
    """老师对某条点『驳回』→ 跑一次 recheck_detection。"""
    tool = RecheckDetectionTool(rechecker=FakeRechecker(verdict), max_rounds=2)
    return await tool.run({"text": TEXT, "detection": det.model_dump(), "round": round_})


def replace_card(detections: list[Detection], out: dict) -> list[Detection]:
    """UI 侧的替换动作：**按 id 就地换对象**，不重排、不重新编号。

    改判时界面展示的就是新改法，因此该条当前的 ``corrected`` 同步为改判后的值。
    """
    updated = Detection.model_validate(out["detection"])
    if updated.recheck is not None and updated.recheck.status == "revised":
        updated = updated.model_copy(update={"corrected": updated.recheck.corrected or ""})
    return [updated if d.id == updated.id else d for d in detections]


class TestIdsStayStable:
    async def test_复核前后其余条目的_id_与顺序不变(self) -> None:
        ds = sample_detections()
        out = await recheck(ds[1], RecheckVerdict(status="upheld"))

        assert out["id"] == "d2"
        after = replace_card(ds, out)

        assert [d.id for d in after] == ["d1", "d2", "d3"]
        # 其余两条连对象都没动
        assert after[0] is ds[0]
        assert after[2] is ds[2]


class TestWithdrawnGoesNowhere:
    async def test_撤销后定稿不含该条(self) -> None:
        ds = sample_detections()
        out = await recheck(
            ds[1], RecheckVerdict(status="withdrawn", reason_zh="这里其实没错"), round_=1
        )

        assert out["status"] == "withdrawn"
        assert out["follow_up"] == "terminal"
        after = replace_card(ds, out)

        warnings: list[str] = []
        # 界面可能还留着旧的『采纳』——撤销是终态，不该生效
        kept = apply_decisions(
            after,
            {"d1": "accepted", "d2": "accepted", "d3": "accepted"},
            warnings=warnings,
        )

        assert [d.id for d in kept] == ["d1", "d3"]
        assert any("已撤销" in w for w in warnings)

        text, _ = finalize_text(TEXT, kept)
        assert "I go to park" in text  # 被撤销的那处原样保留
        assert "very happy" in text and "We were" in text


class TestUpheldKeepsOriginal:
    async def test_维持后老师再采纳用原改法(self) -> None:
        ds = sample_detections()
        out = await recheck(ds[1], RecheckVerdict(status="upheld", reason_zh="确实是过去式"))

        assert out["status"] == "upheld"
        assert out["follow_up"] == "pending_human"
        after = replace_card(ds, out)

        kept = apply_decisions(after, {"d1": "accepted", "d2": "accepted", "d3": "accepted"})

        assert [d.id for d in kept] == ["d1", "d2", "d3"]
        assert kept[1].corrected == "went to"

        text, _ = finalize_text(TEXT, kept)
        assert "I went to park" in text


class TestRevisedUsesNewFix:
    async def test_改判后老师再采纳用新改法(self) -> None:
        ds = sample_detections()
        out = await recheck(
            ds[1],
            RecheckVerdict(
                status="revised",
                corrected="went to the",
                explanation_zh="go to the park 更符合习惯",
                tier="error",
                reason_zh="补上冠词更顺",
            ),
        )

        assert out["status"] == "revised"
        assert out["follow_up"] == "pending_human"
        after = replace_card(ds, out)

        kept = apply_decisions(after, {"d1": "accepted", "d2": "accepted", "d3": "accepted"})

        assert kept[1].corrected == "went to the"

    async def test_改后全文位置不歪(self) -> None:
        ds = sample_detections()
        out = await recheck(ds[1], RecheckVerdict(status="revised", corrected="went to the"))
        after = replace_card(ds, out)

        kept = apply_decisions(after, {"d1": "accepted", "d2": "accepted", "d3": "accepted"})
        text, warnings = finalize_text(TEXT, kept)

        assert warnings == []
        assert text == FINAL_ALL_ACCEPTED


class TestRoundsExhausted:
    async def test_次数用完后工具拒绝_该条降级为剔除(self) -> None:
        ds = sample_detections()

        try:
            await recheck(ds[1], RecheckVerdict(status="upheld"), round_=3)
        except ToolError as exc:
            assert "次数已用完" in str(exc)
        else:  # pragma: no cover - 走到这里说明兜底失效
            raise AssertionError("第 3 次复核应当被兜底拦下")

        # 工具拒绝后界面把『驳回』降级为『剔除此条』：直接置 rejected，不发 run 3
        kept = apply_decisions(ds, {"d1": "accepted", "d2": "rejected", "d3": "accepted"})

        assert [d.id for d in kept] == ["d1", "d3"]

        text, _ = finalize_text(TEXT, kept)
        assert "I go to park" in text

    async def test_第二轮复核的文案带轮次(self) -> None:
        out = await recheck(sample_detections()[1], RecheckVerdict(status="upheld"), round_=2)

        assert out["round"] == 2
        assert "第 2 次" in out["note_zh"]
