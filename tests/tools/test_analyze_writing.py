"""AnalyzeWritingTool 测试：编排 硬指标→词汇分布→模型对照，返回组合结果。

seam = ``AnalyzeWritingTool(analyzer, repo).run({"text": ...})``；analyzer 用 stub
（不触 LLM），repo 指向临时 words/forms db。
"""

import sqlite3
from pathlib import Path

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.analyze_writing import AnalyzeWritingTool
from glyph.tools.writing_analyzer import LevelEvidence, WritingAssessment

_SCHEMA = """
CREATE TABLE words (
  word TEXT PRIMARY KEY, phonetic TEXT, definition TEXT, translation TEXT,
  pos TEXT, collins INTEGER, oxford INTEGER, tag TEXT, bnc INTEGER,
  frq INTEGER, exchange TEXT
);
CREATE TABLE forms (form TEXT PRIMARY KEY, base TEXT);
"""


def _make_db(tmp_path: Path, words: list[tuple]) -> Path:
    db = tmp_path / "dict.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO words (word,phonetic,definition,translation,pos,collins,oxford,"
        "tag,bnc,frq,exchange) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        words,
    )
    con.commit()
    con.close()
    return db


def _W(word: str, trans: str, tag: str, frq: int) -> tuple:
    return (word, "", "", trans, "", 0, 0, tag, 0, frq, "")


class _StubAnalyzer:
    def __init__(
        self,
        result: WritingAssessment | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.captured: tuple | None = None

    async def analyze(self, text, metrics, vocab):
        self.captured = (text, metrics, vocab)
        if self._error is not None:
            raise self._error
        return self._result


def _assessment() -> WritingAssessment:
    return WritingAssessment(
        level_range=("B1", "B2"),
        confidence="medium",
        evidence=[LevelEvidence(quote="we went out", feature="短句", supports="A2")],
        next_level_gap="增加从句",
        warnings=["词汇为近似"],
    )


PASSAGE = (
    "Last weekend I went to the park with my friends. "
    "Although the weather was not very good, we played games and had a lot of fun. "
    "I hope we can go there again soon."
)


async def test_run_combines_metrics_vocab_and_assessment(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path,
        [
            _W("went", "v. 去", "cet4", 400),
            _W("although", "conj. 尽管", "cet6", 3000),
            _W("weather", "n. 天气", "cet4", 1500),
        ],
    )
    stub = _StubAnalyzer(_assessment())
    out = await AnalyzeWritingTool(stub, db).run({"text": PASSAGE})

    assert out["level_range"] == ["B1", "B2"]
    assert out["confidence"] == "medium"
    assert out["next_level_gap"] == "增加从句"
    # 确定性证据也在输出里，前端可展示、用户可复核
    assert out["metrics"]["sentence_count"] == 3
    assert out["vocabulary"]["total"] > 0
    # analyzer 确实收到了算好的 metrics 与 vocab
    _, metrics, vocab = stub.captured
    assert metrics.sentence_count == 3
    assert vocab["total"] == metrics.word_count


async def test_too_short_text_is_rejected(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("hi", "你好", "gk", 200)])
    with pytest.raises(ToolError):
        await AnalyzeWritingTool(_StubAnalyzer(_assessment()), db).run({"text": "Hi"})


async def test_analyzer_error_propagates(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("went", "去", "cet4", 400)])
    stub = _StubAnalyzer(error=ToolError("模型调用失败：boom"))
    with pytest.raises(ToolError):
        await AnalyzeWritingTool(stub, db).run({"text": PASSAGE})


async def test_empty_text_raises(tmp_path: Path) -> None:
    db = _make_db(tmp_path, [_W("go", "去", "cet4", 400)])
    with pytest.raises(ToolError):
        await AnalyzeWritingTool(_StubAnalyzer(_assessment()), db).run({"text": "  ... "})
