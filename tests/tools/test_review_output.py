"""定稿产物的离线测试（纯函数，不联网）：清单文本 + 批阅稿分段。

这里钉死的是**收尾口径**：

* ``build_result_text`` —— 清单模板由后端单点产出，前端不再各拼一份；
* ``build_marked_segments`` —— 把批改痕迹印回原文（划掉 + 顶改法 + 编号），
  这是清单做不到、而我们有字符位置才做得到的事。

三条铁律：
1. **编号必须一致**：清单里的 1、2、3 与批阅稿里的上标编号必须指同一条；
   印不上的条目只能排最后，**绝不能抢占编号**（否则对着批阅稿看讲解会看错行）；
2. **原文绝不被改动**：所有 plain + removed 段拼起来必须一字不差等于原文；
3. 划掉的必须是**原文切片**，不是 ``detection.original``。

区间一律用 ``text.index()`` 定位，不手算索引（手算错过一次）。
"""

from __future__ import annotations

from glyph.tools.grading_models import Detection
from glyph.tools.review import Mark, build_marked_segments, build_result_text

TEXT = "Last weekend was happy. I go to park with my mum."


def det(
    did: str,
    original: str,
    corrected: str,
    start: int | None = None,
    end: int | None = None,
    aligned: bool = True,
    explanation_zh: str = "",
) -> Detection:
    return Detection(
        id=did,
        original=original,
        corrected=corrected,
        start=start,
        end=end,
        aligned=aligned,
        explanation_zh=explanation_zh,
    )


def span(word: str) -> tuple[int, int]:
    """按原文实际位置取区间，不手算。"""
    i = TEXT.index(word)
    return i, i + len(word)


def plain_text(marks: list[Mark]) -> str:
    """把所有 plain + removed 段拼回去——应当一字不差等于原文。"""
    return "".join(m.text for m in marks if m.kind in ("plain", "removed"))


class TestBuildResultText:
    def test_没有定稿条目时说明没保留修改(self) -> None:
        out = build_result_text(TEXT, [])

        assert out.startswith("【批改结果】")
        assert "本轮没有保留任何修改" in out

    def test_单条产出编号与讲解(self) -> None:
        s, e = span("go")
        d = det("d1", "go", "went", s, e, explanation_zh="过去式")
        out = build_result_text(TEXT, [d])

        assert "1. go → went" in out
        assert "讲解：过去式" in out

    def test_按位置顺序而不是传入顺序(self) -> None:
        """清单与批阅稿编号必须一致，所以都以**原文位置**为准。"""
        s_go, e_go = span("go")
        s_happy, e_happy = span("happy")
        # 故意倒着传：go（26 位）在前、happy（17 位）在后
        out = build_result_text(
            TEXT,
            [
                det("d2", "go", "went", s_go, e_go),
                det("d1", "happy", "glad", s_happy, e_happy),
            ],
        )

        assert out.index("happy → glad") < out.index("go → went")

    def test_改后采纳标出手动修改(self) -> None:
        s, e = span("go")
        out = build_result_text(TEXT, [det("d1", "go", "went to the", s, e)], edited_ids={"d1"})

        assert "（手动修改）" in out

    def test_未标为手动修改的没有标记(self) -> None:
        s, e = span("go")
        out = build_result_text(TEXT, [det("d1", "go", "went", s, e)])

        assert "（手动修改）" not in out

    def test_评语附加在末尾(self) -> None:
        s, e = span("go")
        out = build_result_text(TEXT, [det("d1", "go", "went", s, e)], comment="注意时态")

        assert out.rstrip().endswith("整体评语：注意时态")

    def test_没有评语就不出现整体评语(self) -> None:
        out = build_result_text(TEXT, [], comment="   ")

        assert "整体评语" not in out

    def test_没有讲解就不出现讲解行(self) -> None:
        s, e = span("go")
        out = build_result_text(TEXT, [det("d1", "go", "went", s, e)])

        assert "讲解：" not in out

    def test_定位不到的条目仍留在清单里(self) -> None:
        """进不了批阅稿（没位置），但讲解还得让人看到。"""
        d = det("d1", "某处", "另一处", None, None, aligned=False, explanation_zh="通病")
        out = build_result_text(TEXT, [d])

        assert "1. 某处 → 另一处" in out
        assert "讲解：通病" in out


class TestMarkedSegments:
    def test_没有改动时只有一段原文(self) -> None:
        marks, warnings = build_marked_segments(TEXT, [])

        assert [(m.kind, m.text) for m in marks] == [("plain", TEXT)]
        assert warnings == []

    def test_单条切出原文_删_增_原文(self) -> None:
        s, e = span("go")
        marks, warnings = build_marked_segments(TEXT, [det("d1", "go", "went", s, e)])

        assert [m.kind for m in marks] == ["plain", "removed", "added", "plain"]
        assert marks[1].text == "go"
        assert marks[2].text == "went"
        assert warnings == []

    def test_划掉的是原文切片而不是_original_字段(self) -> None:
        """original 可能与实际区间对不上，划掉的必须是原文里真有的那几个字符。"""
        s, e = span("go")
        # 故意把 original 写错
        marks, _ = build_marked_segments(TEXT, [det("d1", "goes", "went", s, e)])

        assert marks[1].text == "go"

    def test_所有非新增段拼回等于原文(self) -> None:
        s1, e1 = span("happy")
        s2, e2 = span("mum")
        marks, _ = build_marked_segments(
            TEXT,
            [det("d1", "happy", "glad", s1, e1), det("d2", "mum", "mother", s2, e2)],
        )

        assert plain_text(marks) == TEXT

    def test_编号按位置递增并带上条目_id(self) -> None:
        s_happy, e_happy = span("happy")
        s_go, e_go = span("go")
        # 故意倒着传：go（26 位）在前、happy（17 位）在后
        marks, _ = build_marked_segments(
            TEXT,
            [
                det("d2", "go", "went", s_go, e_go),
                det("d1", "happy", "glad", s_happy, e_happy),
            ],
        )
        added = [m for m in marks if m.kind == "added"]

        assert [(m.index, m.detection_id) for m in added] == [(1, "d1"), (2, "d2")]

    def test_未对齐的条目不进分段但进提示(self) -> None:
        marks, warnings = build_marked_segments(
            TEXT, [det("d1", "go", "went", None, None, aligned=False)]
        )

        assert [m.kind for m in marks] == ["plain"]
        assert any("未能定位到原文位置" in w for w in warnings)

    def test_区间越界不进分段(self) -> None:
        marks, warnings = build_marked_segments(TEXT, [det("d1", "go", "went", 900, 905)])

        assert [m.kind for m in marks] == ["plain"]
        assert any("区间不合法" in w for w in warnings)

    def test_区间重叠时跳过并提示(self) -> None:
        s, e = span("happy")
        marks, warnings = build_marked_segments(
            TEXT,
            [
                det("d1", "happy", "glad", s, e),
                det("d2", "happy.", "joyful", s, e + 1),
            ],
        )

        assert [m.index for m in marks if m.kind == "added"] == [1]
        assert any("重叠" in w for w in warnings)

    def test_改法为空时只划掉不新增(self) -> None:
        s, e = span("happy")
        marks, _ = build_marked_segments(TEXT, [det("d1", "happy", "", s, e)])

        assert [m.kind for m in marks] == ["plain", "removed", "plain"]

    def test_改动在开头与结尾都不越界(self) -> None:
        marks, _ = build_marked_segments("I has a book", [det("d1", "has", "have", 2, 5)])

        assert plain_text(marks) == "I has a book"
        assert [m.kind for m in marks] == ["plain", "removed", "added", "plain"]


class TestNumberingStaysAligned:
    """清单编号与批阅稿上标编号必须指同一条——这是最容易悄悄坏掉的一条。"""

    def test_编号与清单顺序一致(self) -> None:
        s_happy, e_happy = span("happy")
        s_go, e_go = span("go")
        kept = [
            det("d1", "happy", "glad", s_happy, e_happy, explanation_zh="形容词"),
            det("d2", "go", "went", s_go, e_go, explanation_zh="时态"),
        ]
        marks, _ = build_marked_segments(TEXT, kept)
        out = build_result_text(TEXT, kept)

        first_id = next(m.detection_id for m in marks if m.index == 1)

        assert first_id == "d1"
        assert out.splitlines()[1].startswith("1. happy → glad")

    def test_印不上的条目不得抢占编号(self) -> None:
        """印不上的条目混在前面，会把两处的编号搞串。"""
        s_happy, e_happy = span("happy")
        s_go, e_go = span("go")
        # d1 位置更靠前，但没对齐 → 印不上，只能排最后
        kept = [
            det("d1", "happy", "glad", s_happy, e_happy, aligned=False),
            det("d2", "go", "went", s_go, e_go),
        ]
        marks, _ = build_marked_segments(TEXT, kept)
        out = build_result_text(TEXT, kept)

        added = [m for m in marks if m.kind == "added"]

        assert [(m.index, m.detection_id) for m in added] == [(1, "d2")]  # 批阅稿 ① = go
        assert out.splitlines()[1].startswith("1. go → went")  # 清单第 1 条也是它
        assert out.splitlines()[2].startswith("2. happy → glad")  # 印不上的挪到最后

    def test_空区间的条目同样不抢占编号(self) -> None:
        """另一种"印不上"：区间合法范围但 start == end。"""
        s_go, e_go = span("go")
        kept = [
            det("d1", "空", "改法", 10, 10),
            det("d2", "go", "went", s_go, e_go),
        ]
        marks, warnings = build_marked_segments(TEXT, kept)
        out = build_result_text(TEXT, kept)

        added = [m for m in marks if m.kind == "added"]

        assert [(m.index, m.detection_id) for m in added] == [(1, "d2")]
        assert out.splitlines()[1].startswith("1. go → went")
        assert any("区间不合法" in w for w in warnings)
