"""逐条裁决与定稿的离线测试（纯函数，不联网）。

这里钉死的是**定稿口径**：采纳 + 改成我的进定稿，驳回/取消/没表态一律不进；
改后全文必须按区间精确套回，位置一个字符都不能歪。
"""

from __future__ import annotations

from glyph.tools.grading_models import Detection, RecheckInfo
from glyph.tools.review import apply_decisions, finalize_text

TEXT = "Last weekend was happy. I go to park with my mum."


def det(
    did: str,
    original: str,
    corrected: str,
    start: int | None = None,
    end: int | None = None,
    aligned: bool = True,
) -> Detection:
    return Detection(
        id=did,
        original=original,
        corrected=corrected,
        start=start,
        end=end,
        aligned=aligned,
    )


class TestApplyDecisions:
    def test_采纳原样进定稿(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        kept = apply_decisions([d], {"d1": "accepted"})

        assert [x.id for x in kept] == ["d1"]
        assert kept[0].corrected == "went"

    def test_改后采纳用人的改法(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        kept = apply_decisions([d], {"d1": "edited"}, {"d1": "went to the"})

        assert kept[0].corrected == "went to the"

    def test_改后采纳但没给改法则保留原改法(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        kept = apply_decisions([d], {"d1": "edited"})

        assert kept[0].corrected == "went"

    def test_驳回与取消都剔除(self) -> None:
        ds = [det("d1", "go", "went"), det("d2", "mum", "mother")]
        kept = apply_decisions(ds, {"d1": "rejected", "d2": "cancelled"})

        assert kept == []

    def test_没表态的条目剔除_保守(self) -> None:
        ds = [det("d1", "go", "went"), det("d2", "mum", "mother")]
        kept = apply_decisions(ds, {"d1": "accepted"})

        assert [x.id for x in kept] == ["d1"]

    def test_没有_id_的条目剔除(self) -> None:
        ds = [det("", "go", "went")]
        kept = apply_decisions(ds, {"": "accepted"})

        assert kept == []

    def test_原条目不被就地修改(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        kept = apply_decisions([d], {"d1": "edited"}, {"d1": "gone"})

        assert d.corrected == "went"  # 原对象不变
        assert kept[0].corrected == "gone"

    def test_混合场景只留采纳与改后采纳(self) -> None:
        ds = [det(f"d{i}", f"o{i}", f"c{i}") for i in range(1, 5)]
        kept = apply_decisions(
            ds,
            {"d1": "accepted", "d2": "rejected", "d3": "edited", "d4": "cancelled"},
            {"d3": "人写的改法"},
        )

        assert [x.id for x in kept] == ["d1", "d3"]
        assert kept[1].corrected == "人写的改法"


class TestRecheckContract:
    """复核字段**只增不改**：不得影响 recheck 为 None 时今天的定稿口径（R-0 兼容）。"""

    def test_默认没有复核结论(self) -> None:
        assert Detection(original="go", corrected="went", id="d1").recheck is None

    def test_挂上复核结论后仍是同一条_不换_id(self) -> None:
        d = Detection(
            original="go",
            corrected="went",
            id="d1",
            recheck=RecheckInfo(
                status="upheld", round=1, scope="sentence", note_zh="仍是过去式问题"
            ),
        )

        assert d.id == "d1"
        assert d.recheck is not None
        assert d.recheck.status == "upheld"

    def test_有复核结论不改变定稿口径(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        d.recheck = RecheckInfo(status="revised", round=1, scope="sentence", corrected="had gone")

        kept = apply_decisions([d], {"d1": "accepted"})

        # 改判的新改法要等老师再拍板才生效，此刻定稿仍用原 corrected
        assert kept[0].corrected == "went"

    def test_维持原判的条目仍按老师表态走(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        d.recheck = RecheckInfo(status="upheld", round=1, corrected="went")

        kept = apply_decisions([d], {"d1": "accepted"})

        assert [x.id for x in kept] == ["d1"]

    def test_已撤销的条目不进定稿_即使界面还留着采纳(self) -> None:
        """C-2 防御：AI 自己都认错了，旧的『采纳』不该再生效。"""
        d = det("d1", "go", "went", 26, 28)
        d.recheck = RecheckInfo(status="withdrawn", round=2)

        warnings: list[str] = []
        kept = apply_decisions([d], {"d1": "accepted"}, warnings=warnings)

        assert kept == []
        assert any("已撤销" in w for w in warnings)

    def test_没有撤销就不记提示(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        warnings: list[str] = []
        apply_decisions([d], {"d1": "accepted"}, warnings=warnings)

        assert warnings == []


class TestFinalizeText:
    def test_单条改动精确套回(self) -> None:
        d = det("d1", "go", "went", 26, 28)
        new_text, warnings = finalize_text(TEXT, [d])

        assert new_text == "Last weekend was happy. I went to park with my mum."
        assert warnings == []

    def test_多条改动逆序套回_位置不漂移(self) -> None:
        # 两条一前一后：若按正序替换，第二条会被第一条推歪
        ds = [
            det("d1", "happy", "very happy", 17, 22),
            det("d2", "go", "went", 26, 28),
        ]
        new_text, warnings = finalize_text(TEXT, ds)

        assert new_text == "Last weekend was very happy. I went to park with my mum."
        assert warnings == []

    def test_没有定稿条目时原文原样返回(self) -> None:
        new_text, warnings = finalize_text(TEXT, [])

        assert new_text == TEXT
        assert warnings == []

    def test_未对齐的条目不自动套用_但保留讲解(self) -> None:
        d = det("d1", "go", "went", None, None, aligned=False)
        new_text, warnings = finalize_text(TEXT, [d])

        assert new_text == TEXT
        assert len(warnings) == 1
        assert "未能定位到原文位置" in warnings[0]

    def test_区间越界不套用(self) -> None:
        d = det("d1", "go", "went", 900, 905)
        new_text, warnings = finalize_text(TEXT, [d])

        assert new_text == TEXT
        assert any("区间不合法" in w for w in warnings)

    def test_区间重叠时跳过并提示(self) -> None:
        ds = [
            det("d1", "happy", "glad", 17, 22),
            det("d2", "happy.", "joyful", 17, 23),
        ]
        new_text, warnings = finalize_text(TEXT, ds)

        assert new_text == "Last weekend was glad. I go to park with my mum."
        assert any("重叠" in w for w in warnings)

    def test_整链路_裁决后直接出改后全文(self) -> None:
        ds = [
            det("d1", "happy", "very happy", 17, 22),
            det("d2", "go", "went", 26, 28),
            det("d3", "mum", "mother", 43, 46),
        ]
        kept = apply_decisions(
            ds, {"d1": "accepted", "d2": "edited", "d3": "rejected"}, {"d2": "went"}
        )
        new_text, warnings = finalize_text(TEXT, kept)

        # d3 被驳回，mum 必须原样保留
        assert new_text == "Last weekend was very happy. I went to park with my mum."
        assert warnings == []
