"""polish_text 工具测试：编排「本地切句/查词 → 模型出改写 → 本地对齐/去重/限条/定级」。

seam 分三层，各自独立可测：

- 纯函数层 ``polish_rules``：锚点对齐、去重、限条、升级词定级（离线，不触 LLM）；
- 模型层 ``polish_analyzer``：严格 JSON 解析 + 提示词契约；
- 工具层 ``polish``：编排、失败分级、取消传播、反谄媚。

LLM 一律用 stub 注入，不联网（R6）。
"""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from glyph.runtime.errors import ToolError
from glyph.tools.dict_repo import DictRepository
from glyph.tools.polish import PolishTextTool
from glyph.tools.polish_analyzer import parse_polish_content
from glyph.tools.polish_models import PolishDraft, Suggestion, Upgrade
from glyph.tools.polish_rules import (
    MAX_SUGGESTIONS,
    annotate_upgrades,
    finalize_suggestions,
    normalize_issue_kind,
)

# ---------------------------------------------------------------------------
# 临时词典（照 test_analyze_writing.py 的 schema；升级词的等级一律本地查，不靠模型）
# ---------------------------------------------------------------------------

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


@pytest.fixture
def repo(tmp_path: Path) -> DictRepository:
    db = _make_db(
        tmp_path,
        [
            _W("compelling", "a. 引人入胜的", "cet6 ky", 4000),
            _W("great", "a. 很好的", "gk", 300),
            _W("utilize", "vt. 利用", "ky", 6000),
        ],
    )
    return DictRepository(db)


TEXT = "I think this is a very good idea. It is also very useful."


def _sug(
    original: str,
    *,
    safe: str = "I think this is a great idea.",
    advanced: str = "I find this idea compelling.",
    issue: str = "词太泛",
    issue_kind: str = "word_precision",
    why: str = "very good 太泛；compelling 含义更精确",
    upgrades: list[Upgrade] | None = None,
) -> Suggestion:
    return Suggestion(
        original=original,
        issue=issue,
        issue_kind=issue_kind,
        safe=safe,
        advanced=advanced,
        why=why,
        upgrades=upgrades or [],
    )


# ---------------------------------------------------------------------------
# 纯函数层：锚点对齐 / 去重 / 限条
# ---------------------------------------------------------------------------


def test_finalize_keeps_located_suggestion():
    """模型给的原句能在原文里找到 → 保留，并带上真实位置（供前端高亮对照）。"""
    kept, warnings = finalize_suggestions([_sug("I think this is a very good idea.")], TEXT)
    assert len(kept) == 1
    assert kept[0].start == 0
    assert kept[0].end == len("I think this is a very good idea.")
    assert warnings == []


def test_finalize_drops_suggestion_whose_original_is_not_in_text():
    """模型编出来的原句（原文里根本没有）必须丢弃 —— 留着用户对不上，是噪声。"""
    kept, warnings = finalize_suggestions([_sug("This sentence does not exist at all.")], TEXT)
    assert kept == []
    assert any("找不到" in w for w in warnings)


def test_finalize_deduplicates_same_original():
    """同一处被模型挑了两遍 → 只留一条。"""
    kept, _ = finalize_suggestions(
        [_sug("a very good idea"), _sug("a very good idea", safe="x", advanced="y")], TEXT
    )
    assert len(kept) == 1


def test_finalize_drops_positionally_overlapping_suggestion():
    """位置重叠的两条（嵌套的片段）只留第一条 —— 否则同一处给两个改法，用户无所适从。"""
    kept, _ = finalize_suggestions(
        [
            _sug("very good"),
            _sug("a very good idea"),
        ],
        TEXT,
    )
    assert len(kept) == 1
    assert kept[0].original == "very good"


def test_finalize_caps_at_max_suggestions():
    """超过上限就截断并记一条 warning（不静默砍）。"""
    text = ". ".join(f"Sentence number {i} is quite fine" for i in range(8)) + "."
    many = [_sug(f"Sentence number {i} is quite fine") for i in range(8)]
    kept, warnings = finalize_suggestions(many, text)
    assert len(kept) == MAX_SUGGESTIONS
    assert any("上限" in w or "最多" in w for w in warnings)


def test_finalize_empty_input_stays_empty_without_warning():
    """模型说「没得改」→ 空列表、不出 warning（这是正常结论，不是异常）。"""
    kept, warnings = finalize_suggestions([], TEXT)
    assert kept == []
    assert warnings == []


# ---------------------------------------------------------------------------
# 纯函数层：issue_kind 归位 与 升级词定级
# ---------------------------------------------------------------------------


def test_normalize_issue_kind_accepts_known_values():
    assert normalize_issue_kind("word_precision") == "word_precision"
    assert normalize_issue_kind("sentence_tightness") == "sentence_tightness"
    assert normalize_issue_kind("register") == "register"


def test_normalize_issue_kind_falls_back_for_unknown():
    """模型给了枚举外的词 → 归到默认，绝不把未知值透传给前端。"""
    assert normalize_issue_kind("mystery") == "word_precision"
    assert normalize_issue_kind("") == "word_precision"
    assert normalize_issue_kind(None) == "word_precision"


def test_annotate_upgrades_reads_level_from_local_dict(repo: DictRepository):
    """升级词的考试标签与等级一律本地查 —— 这是本工具「有据」的全部来源。"""
    ups = annotate_upgrades([Upgrade(source="very good", target="compelling")], repo)
    assert len(ups) == 1
    assert ups[0].target == "compelling"
    assert "CET6" in ups[0].tags
    assert "考研" in ups[0].tags
    assert ups[0].level_approx == "B2"  # cet6/ky 近似映射


def test_annotate_upgrades_leaves_unknown_word_unlabelled(repo: DictRepository):
    """查不到的词（短语、专有名词）→ 标签留空、等级留空，**绝不编一个**。"""
    ups = annotate_upgrades([Upgrade(source="x", target="to be worth doing")], repo)
    assert ups[0].tags == []
    assert ups[0].level_approx is None


# ---------------------------------------------------------------------------
# 模型层：严格 JSON 解析
# ---------------------------------------------------------------------------


def _payload(**over) -> str:
    data = {
        "suggestions": [
            {
                "original": "I think this is a very good idea.",
                "issue": "词太泛",
                "issue_kind": "word_precision",
                "safe": "I think this is a great idea.",
                "advanced": "I find this idea compelling.",
                "why": "very good 太泛",
                "upgrades": [{"from": "very good", "to": "compelling"}],
            }
        ],
        "nothing_to_polish": False,
        "overall": "表达基本清楚，个别用词可以更精确。",
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


def test_parse_valid_json():
    draft = parse_polish_content(_payload())
    assert isinstance(draft, PolishDraft)
    assert len(draft.suggestions) == 1
    s = draft.suggestions[0]
    assert s.safe == "I think this is a great idea."
    assert s.advanced == "I find this idea compelling."
    assert s.upgrades == [Upgrade(source="very good", target="compelling")]
    assert draft.nothing_to_polish is False


def test_parse_drops_suggestion_without_original():
    """没有原句锚点的建议无法核对，直接丢弃（与 analyze_writing 的 evidence 同一口径）。"""
    draft = parse_polish_content(
        _payload(
            suggestions=[
                {"original": "", "safe": "a", "advanced": "b", "why": "c"},
                {
                    "original": "I think this is a very good idea.",
                    "safe": "a",
                    "advanced": "b",
                    "why": "c",
                },
            ]
        )
    )
    assert len(draft.suggestions) == 1


def test_parse_reports_nothing_to_polish():
    """模型认为没得改 → nothing_to_polish 为真、建议为空（反谄媚的契约面）。"""
    draft = parse_polish_content(
        json.dumps(
            {"suggestions": [], "nothing_to_polish": True, "overall": "这段表达已经到位。"},
            ensure_ascii=False,
        )
    )
    assert draft.suggestions == []
    assert draft.nothing_to_polish is True
    assert draft.overall


def test_parse_bad_json_raises():
    with pytest.raises((json.JSONDecodeError, ValueError, TypeError)):
        parse_polish_content("这不是 JSON")


# ---------------------------------------------------------------------------
# 工具层：编排 / 失败分级 / 取消 / 反谄媚
# ---------------------------------------------------------------------------


class _StubPolisher:
    def __init__(self, draft: PolishDraft | None = None, error: BaseException | None = None):
        self._draft = draft
        self._error = error
        self.captured: str | None = None

    async def polish(self, text, metrics):
        self.captured = text
        if self._error is not None:
            raise self._error
        assert self._draft is not None
        return self._draft


def _draft(*sugs: Suggestion, overall: str = "总评", nothing: bool = False) -> PolishDraft:
    return PolishDraft(suggestions=list(sugs), overall=overall, nothing_to_polish=nothing)


async def test_run_returns_aligned_suggestions(repo: DictRepository):
    stub = _StubPolisher(
        _draft(
            _sug(
                "I think this is a very good idea.",
                upgrades=[Upgrade(source="very good", target="compelling")],
            )
        )
    )
    out = await PolishTextTool(polisher=stub, repo=repo).run({"text": TEXT})

    assert len(out["suggestions"]) == 1
    s = out["suggestions"][0]
    assert s["original"] == "I think this is a very good idea."
    assert s["safe"] and s["advanced"] and s["why"]
    assert s["start"] == 0
    # 升级词等级来自本地词典（硬标签 + 近似等级），不是模型说的
    assert s["upgrades"][0]["to"] == "compelling"
    assert s["upgrades"][0]["tags"] == ["CET6", "考研"]
    assert s["upgrades"][0]["level_approx"] == "B2"
    assert out["nothing_to_polish"] is False
    # 等级是近似：notes 必须带上这句限定，不许把近似包装成权威
    assert any("近似" in n for n in out["notes"])
    assert stub.captured == TEXT


async def test_run_says_nothing_to_polish_when_model_finds_none(repo: DictRepository):
    """反谄媚：模型说没得改 → 如实回报，不硬凑建议。"""
    stub = _StubPolisher(_draft(nothing=True, overall="这段表达已经到位，没有非改不可的地方。"))
    out = await PolishTextTool(polisher=stub, repo=repo).run({"text": TEXT})
    assert out["suggestions"] == []
    assert out["nothing_to_polish"] is True
    assert out["overall"]


async def test_run_drops_hallucinated_original_and_warns(repo: DictRepository):
    """模型编的原句被丢弃后，warning 要透出来，别让工具假装一切正常。"""
    stub = _StubPolisher(_draft(_sug("A sentence that is not in the text.")))
    out = await PolishTextTool(polisher=stub, repo=repo).run({"text": TEXT})
    assert out["suggestions"] == []
    assert any("找不到" in w for w in out["warnings"])


async def test_dropped_suggestions_are_not_reported_as_nothing_to_polish(repo: DictRepository):
    """「建议全被剔除」≠「没得改」。

    前者是异常（模型编了原句），后者是个结论。把前者说成后者，等于对用户撒谎
    「这段表达已经到位」—— 必须靠 warning 说清，不能伪装成结论（设计文档 §2.3）。
    """
    stub = _StubPolisher(_draft(_sug("A sentence that is not in the text.")))
    out = await PolishTextTool(polisher=stub, repo=repo).run({"text": TEXT})
    assert out["nothing_to_polish"] is False
    assert out["warnings"]


async def test_empty_text_rejected(repo: DictRepository):
    with pytest.raises(ToolError):
        await PolishTextTool(polisher=_StubPolisher(_draft()), repo=repo).run({"text": "   "})


async def test_too_short_text_rejected(repo: DictRepository):
    """少于 1 句 / 5 词：没有可升档的余地，直接报错而不是硬给建议。"""
    with pytest.raises(ToolError):
        await PolishTextTool(polisher=_StubPolisher(_draft()), repo=repo).run({"text": "Good."})


async def test_polisher_error_propagates(repo: DictRepository):
    stub = _StubPolisher(error=ToolError("润色模型调用失败：boom"))
    with pytest.raises(ToolError):
        await PolishTextTool(polisher=stub, repo=repo).run({"text": TEXT})


async def test_cancellation_propagates_untouched(repo: DictRepository):
    """取消必须原样穿透，绝不能被包成 ToolError —— 否则主循环的协作式取消会失配。"""
    stub = _StubPolisher(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await PolishTextTool(polisher=stub, repo=repo).run({"text": TEXT})
