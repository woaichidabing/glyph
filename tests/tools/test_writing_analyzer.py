"""LLMWritingAnalyzer 测试（离线，注入假 client）。

seam = ``LLMWritingAnalyzer(client, model).analyze(text, metrics, vocab)``。
重点：模型只"对照证据"，输出等级区间 + 引用原文的证据；脏字段保守兜底。
"""

import json

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.writing_analyzer import LLMWritingAnalyzer
from glyph.tools.writing_metrics import analyze_text


class _Msg:
    def __init__(self, content) -> None:
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason="stop") -> None:
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Resp:
    def __init__(self, content, finish_reason="stop") -> None:
        self.choices = [_Choice(content, finish_reason)]


class _Scripted:
    def __init__(self, steps) -> None:
        self.steps = list(steps)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self.steps[len(self.calls) - 1]
        if isinstance(step, Exception):
            raise step
        return step


class _Client:
    def __init__(self, *steps) -> None:
        self._completions = _Scripted(steps)

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self._completions


def _payload(**over) -> str:
    data = {
        "level_low": "B1",
        "level_high": "B2",
        "confidence": "medium",
        "evidence": [
            {
                "quote": "Although it rained, we went out",
                "feature": "although 让步从句 + 长短句结合",
                "supports": "B2",
            }
        ],
        "next_level_gap": "增加更多从句类型与正式衔接词",
        "warnings": [],
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


def _metrics(text: str):
    return analyze_text(text)


def _vocab():
    return {
        "total": 10,
        "counts": {"A1": 2, "A2": 3, "B1": 3, "B2": 1, "C1": 1, "C2": 0},
        "percent": {"A1": 20.0, "A2": 30.0, "B1": 30.0, "B2": 10.0, "C1": 10.0, "C2": 0.0},
        "unknown_count": 0,
        "unknown_percent": 0.0,
    }


async def test_parses_valid_assessment() -> None:
    client = _Client(_Resp(_payload()))
    a = await LLMWritingAnalyzer(client, model="m").analyze(
        "Although it rained, we went out.", _metrics("Although it rained, we went out."), _vocab()
    )
    assert a.level_range == ("B1", "B2")
    assert a.confidence == "medium"
    assert a.evidence[0].quote == "Although it rained, we went out"
    assert "从句" in a.evidence[0].feature
    assert "衔接" in a.next_level_gap


async def test_reversed_range_is_swapped() -> None:
    payload = _payload(level_low="C1", level_high="A2")
    client = _Client(_Resp(payload))
    a = await LLMWritingAnalyzer(client, model="m").analyze("x", _metrics("I run."), _vocab())
    low, high = a.level_range
    assert low <= high, "区间必须 low<=high"
    assert low == "A2" and high == "C1"


async def test_bad_confidence_falls_to_low() -> None:
    client = _Client(_Resp(_payload(confidence="maybe??")))
    a = await LLMWritingAnalyzer(client, model="m").analyze("x", _metrics("I run."), _vocab())
    assert a.confidence == "low"


async def test_evidence_without_quote_is_dropped() -> None:
    payload = _payload(
        evidence=[
            {"quote": "", "feature": "x", "supports": "B2"},
            {"quote": "we went out", "feature": "短句", "supports": "A2"},
        ]
    )
    client = _Client(_Resp(payload))
    a = await LLMWritingAnalyzer(client, model="m").analyze("x", _metrics("we went out."), _vocab())
    assert [e.quote for e in a.evidence] == ["we went out"]


async def test_bad_json_retries_then_error() -> None:
    client = _Client(_Resp("{nope"), _Resp("{still nope"))
    with pytest.raises(ToolError):
        await LLMWritingAnalyzer(client, model="m").analyze("x", _metrics("I run."), _vocab())


async def test_keeps_vocab_approximation_warning() -> None:
    # 词汇等级是 ECDICT 近似、非官方 CEFR 词汇表：即使模型没写，也必须保留该提醒
    client = _Client(_Resp(_payload(warnings=[])))
    a = await LLMWritingAnalyzer(client, model="m").analyze("x", _metrics("I run."), _vocab())
    assert any(("近似" in w or "非官方" in w or "ECDICT" in w) for w in a.warnings)
