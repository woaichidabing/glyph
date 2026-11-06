"""复核归一化的离线测试（设计文档 §1.7 的 N-1 ~ N-7，逐条对号入座）。

不联网（R6）。这里钉死两件事：① **拿不准就撤，不维持**；② **复核不是重扫**。
"""

from __future__ import annotations

from glyph.tools.grading_models import Detection, RecheckInfo, effective_error_code
from glyph.tools.recheck import (
    NOTE_TEMPLATES,
    RecheckDetectionTool,
    RecheckRequest,
    RecheckVerdict,
    normalize_verdict,
)

TEXT = "Yesterday I stayed at home. He go to school every day with his friends. It was fine."


def target() -> Detection:
    start = TEXT.index("go")
    return Detection(
        id="d3",
        original="go",
        corrected="went",
        type="hard",
        tier="error",
        explanation_zh="过去的事要用过去式",
        start=start,
        end=start + 2,
        aligned=True,
    )


def normalize(verdict: RecheckVerdict, round_: int = 1, det: Detection | None = None):
    return normalize_verdict(verdict, det or target(), round_, "sentence")


class TestEffectiveErrorCode:
    """D17：知识点代码只对"最终裁决 hard+error"有效；复核撤销 / 改判为非硬错即失效。"""

    def test_未复核硬错保留代码(self) -> None:
        d = target().model_copy(update={"error_code": "VERB:TENSE"})
        assert effective_error_code(d) == "VERB:TENSE"

    def test_未复核非硬错即使带代码也为空(self) -> None:
        d = target().model_copy(
            update={"type": "chinglish", "tier": "suggestion", "error_code": "DET"}
        )
        assert effective_error_code(d) is None

    def test_硬错但无代码为空(self) -> None:
        assert effective_error_code(target()) is None

    def test_复核撤销后代码失效(self) -> None:
        d = target().model_copy(
            update={"error_code": "VERB:TENSE", "recheck": RecheckInfo(status="withdrawn")}
        )
        assert effective_error_code(d) is None

    def test_复核维持则代码保留(self) -> None:
        d = target().model_copy(
            update={"error_code": "VERB:TENSE", "recheck": RecheckInfo(status="upheld")}
        )
        assert effective_error_code(d) == "VERB:TENSE"

    def test_复核改判为非硬错则代码失效(self) -> None:
        d = target().model_copy(
            update={
                "error_code": "VERB:TENSE",
                "recheck": RecheckInfo(
                    status="revised", type="chinglish", tier="suggestion", corrected="..."
                ),
            }
        )
        assert effective_error_code(d) is None

    def test_复核改判仍是硬错则沿用原代码(self) -> None:
        d = target().model_copy(
            update={
                "error_code": "VERB:TENSE",
                "recheck": RecheckInfo(
                    status="revised", type="hard", tier="error", corrected="gone"
                ),
            }
        )
        assert effective_error_code(d) == "VERB:TENSE"


class TestN1UnknownStatus:
    """N-1：status 缺失或不在三态之内 → withdrawn（判断不了就撤，不维持）。"""

    def test_非法状态撤为_withdrawn(self) -> None:
        info, warnings = normalize(RecheckVerdict(status="maybe"))  # type: ignore[arg-type]

        assert info.status == "withdrawn"
        assert any("无法识别" in w for w in warnings)

    def test_空状态也撤(self) -> None:
        info, _ = normalize(RecheckVerdict(status=""))  # type: ignore[arg-type]

        assert info.status == "withdrawn"

    def test_撤销时不携带任何修改建议(self) -> None:
        info, _ = normalize(RecheckVerdict(status="withdrawn", corrected="went"))

        assert info.corrected is None
        assert info.explanation_zh is None
        assert info.tier is None
        assert info.type is None


class TestN2RevisedWithoutChange:
    """N-2：说改判却没实质变化 → 降为 upheld（避免 UI 反复提示"已改判"）。"""

    def test_四项全同降为维持(self) -> None:
        det = target()
        verdict = RecheckVerdict(
            status="revised",
            corrected=det.corrected,
            tier=det.tier,
            type=det.type,
            explanation_zh=det.explanation_zh,
        )
        info, warnings = normalize(verdict, det=det)

        assert info.status == "upheld"
        assert any("没有差别" in w for w in warnings)

    def test_只要有一项真的变了就仍是改判(self) -> None:
        verdict = RecheckVerdict(status="revised", corrected="had gone")
        info, warnings = normalize(verdict)

        assert info.status == "revised"
        assert not any("没有差别" in w for w in warnings)


class TestN3RevisedPartialUpdate:
    """N-3：改判时空值沿用原值，非法枚举沿用原值。"""

    def test_空改法沿用原值(self) -> None:
        info, _ = normalize(RecheckVerdict(status="revised", corrected="   "))

        assert info.corrected == "went"

    def test_非法档位沿用原值(self) -> None:
        info, _ = normalize(
            RecheckVerdict(status="revised", corrected="had gone", tier="super-wrong")
        )

        assert info.tier == "error"

    def test_合法档位照收(self) -> None:
        info, _ = normalize(
            RecheckVerdict(status="revised", corrected="had gone", tier="suggestion")
        )

        assert info.tier == "suggestion"


class TestN4UpheldMeansUnchanged:
    """N-4：维持原判 = 原样维持，模型给的新定性一律不收，只落 reason_zh。"""

    def test_回带原值(self) -> None:
        det = target()
        info, _ = normalize(RecheckVerdict(status="upheld"), det=det)

        assert info.corrected == det.corrected
        assert info.explanation_zh == det.explanation_zh
        assert info.tier == det.tier
        assert info.type == det.type

    def test_模型想顺手改定性也不收(self) -> None:
        info, _ = normalize(
            RecheckVerdict(status="upheld", corrected="偷偷改的", tier="suggestion")
        )

        assert info.corrected == "went"
        assert info.tier == "error"

    def test_模型的理由落到_reason_zh(self) -> None:
        info, _ = normalize(RecheckVerdict(status="upheld", reason_zh="这里确实是过去式"))

        assert info.reason_zh == "这里确实是过去式"


class TestN6Locality:
    """N-6：复核是复核不是重扫——夹带的别处条目全部丢弃并记条数。"""

    def test_不相交的条目被记数(self) -> None:
        verdict = RecheckVerdict(
            status="upheld",
            extra_raw=[{"original": "别的句子里的词", "corrected": "x"}],
        )
        info, warnings = normalize(verdict)

        assert info.status == "upheld"
        assert any("1 条本条目之外" in w for w in warnings)

    def test_多条一起记数(self) -> None:
        verdict = RecheckVerdict(
            status="upheld",
            extra_raw=[{"original": "甲"}, {"original": "乙"}, {"start": 0, "end": 5}],
        )
        _, warnings = normalize(verdict)

        assert any("3 条本条目之外" in w for w in warnings)

    def test_落在同一处的条目不算夹带(self) -> None:
        det = target()
        verdict = RecheckVerdict(
            status="revised",
            corrected="went",
            extra_raw=[{"original": "go", "corrected": "went"}],
        )
        _, warnings = normalize(verdict, det=det)

        assert not any("本条目之外" in w for w in warnings)

    def test_区间相交不算夹带(self) -> None:
        det = target()
        verdict = RecheckVerdict(
            status="upheld",
            extra_raw=[{"start": det.start, "end": det.end}],
        )
        _, warnings = normalize(verdict, det=det)

        assert not any("本条目之外" in w for w in warnings)

    def test_没有夹带就没有提示(self) -> None:
        _, warnings = normalize(RecheckVerdict(status="withdrawn"))

        assert not any("本条目之外" in w for w in warnings)


class TestN7NoteAlwaysPresent:
    """N-7：note_zh 由常量模板生成，三态必非空，不依赖模型输出。"""

    def test_三态文案都非空(self) -> None:
        for status in ("upheld", "withdrawn", "revised"):
            info, _ = normalize(RecheckVerdict(status=status))  # type: ignore[arg-type]
            assert info.note_zh, f"{status} 的文案不能为空"

    def test_非法状态撤回后文案照给(self) -> None:
        info, _ = normalize(RecheckVerdict(status="???"))  # type: ignore[arg-type]

        assert "撤销" in info.note_zh

    def test_文案带轮次(self) -> None:
        info, _ = normalize(RecheckVerdict(status="upheld"), round_=2)

        assert "第 2 次" in info.note_zh

    def test_模板覆盖三态(self) -> None:
        assert set(NOTE_TEMPLATES) == {"upheld", "withdrawn", "revised"}


class TestRoundAndScope:
    def test_轮次与范围回带(self) -> None:
        info, _ = normalize_verdict(RecheckVerdict(status="upheld"), target(), 3, "paragraph")

        assert info.round == 3
        assert info.scope == "paragraph"


class _FakeRechecker:
    """复核器假实现：回固定结论，离线驱动工具层 R1–R7。"""

    def __init__(self, verdict: RecheckVerdict) -> None:
        self._verdict = verdict

    async def review(self, request: RecheckRequest) -> RecheckVerdict:
        return self._verdict


def _recheck_tool(verdict: RecheckVerdict) -> RecheckDetectionTool:
    return RecheckDetectionTool(rechecker=_FakeRechecker(verdict), max_rounds=3)


def _coded_detection_arg() -> dict:
    # 模型把 grade 结果里带知识点码的整条 detection 原样回传给复核工具
    return target().model_copy(update={"error_code": "VERB:TENSE"}).model_dump()


class TestRecheckToolErrorCode:
    """D17 接线：recheck_detection 出口给模型的 error_code 必须是"最终有效码"。

    纯函数 effective_error_code 已在上面钉死判定矩阵，这里钉的是工具出口真的用了它——
    否则撤销 / 改判非硬错后，模型仍会拿着顶层原码去 remember 建账。
    """

    async def test_复核撤销后出口代码清空(self) -> None:
        tool = _recheck_tool(RecheckVerdict(status="withdrawn", reason_zh="我认错了"))
        result = await tool.run({"text": TEXT, "detection": _coded_detection_arg(), "round": 1})
        assert result["status"] == "withdrawn"
        assert result["detection"]["error_code"] is None

    async def test_复核维持则出口代码保留不丢(self) -> None:
        tool = _recheck_tool(RecheckVerdict(status="upheld"))
        result = await tool.run({"text": TEXT, "detection": _coded_detection_arg(), "round": 1})
        assert result["status"] == "upheld"
        assert result["detection"]["error_code"] == "VERB:TENSE"

    async def test_复核改判为非硬错则出口代码清空(self) -> None:
        tool = _recheck_tool(
            RecheckVerdict(
                status="revised",
                corrected="goes",
                type="chinglish",
                tier="suggestion",
                explanation_zh="其实是表达不地道，不算语法硬错",
            )
        )
        result = await tool.run({"text": TEXT, "detection": _coded_detection_arg(), "round": 1})
        assert result["status"] == "revised"
        assert result["detection"]["error_code"] is None

    async def test_复核改判仍是硬错则沿用原代码(self) -> None:
        tool = _recheck_tool(
            RecheckVerdict(status="revised", corrected="gone", type="hard", tier="error")
        )
        result = await tool.run({"text": TEXT, "detection": _coded_detection_arg(), "round": 1})
        assert result["detection"]["error_code"] == "VERB:TENSE"
