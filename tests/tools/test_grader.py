"""批改模型客户端 LLMGrader 测试（离线，注入假 OpenAI client，不联网）。

重点是 JSON 健壮性与失败分类：
- 正常 JSON → 结构化结果；非法 type/tier 保守兜底；
- finish=length 截断 → 自动升到更大 max_tokens 重试，仍截断则 ToolError；
- 非法 JSON → 重试 1 次，仍坏则 ToolError；
- response_format 不支持(400) → 关掉 JSON 模式回退；
- 5xx/连接错可重试，4xx 直接 ToolError 不重试；
- 全对文本 detections 为空也正常。
"""

import json

import pytest

from glyph.memory.mastery import VALID_ERROR_CODES
from glyph.runtime.errors import ToolError
from glyph.tools.grader import LLMGrader, parse_grade_content
from glyph.tools.prompts import SYSTEM_PROMPT


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish_reason):
        self.message = _Msg(content)
        self.finish_reason = finish_reason


class _Resp:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [_Choice(content, finish_reason)]


class _FakeHTTPError(Exception):
    def __init__(self, status_code, message="http error"):
        self.status_code = status_code
        super().__init__(message)


class _ScriptedCompletions:
    """按调用次序返回 _Resp 或抛异常；多调用会越界报错，暴露意外的重试。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        step = self.steps[len(self.calls) - 1]
        if isinstance(step, Exception):
            raise step
        return step


class _Client:
    def __init__(self, steps):
        self.completions = _ScriptedCompletions(steps)

    @property
    def chat(self):
        return self


def _client(*steps) -> _Client:
    return _Client(steps)


def _valid_payload(**over) -> str:
    data = {
        "detections": [
            {
                "original": "I is",
                "corrected": "I am",
                "type": "hard",
                "tier": "error",
                "explanation_zh": "主谓不一致，I 搭配 am",
            }
        ],
        "summary": "整体不错",
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


async def test_grade_parses_valid_json():
    client = _client(_Resp(_valid_payload()))
    result = await LLMGrader(client, model="deepseek-v4-pro").grade("I is a student.")

    assert result.summary == "整体不错"
    [d] = result.detections
    assert d.original == "I is" and d.corrected == "I am"
    assert d.type == "hard" and d.tier == "error" and d.source == "model"
    assert d.start is None  # 模型不回位置，交给本地对齐


async def test_grade_unknown_type_tier_fall_back_conservatively():
    payload = json.dumps(
        {
            "detections": [
                {
                    "original": "x",
                    "corrected": "y",
                    "type": "weird",
                    "tier": "BLAH",
                    "explanation_zh": "?",
                }
            ],
            "summary": "",
        },
        ensure_ascii=False,
    )
    result = await LLMGrader(_client(_Resp(payload)), model="m").grade("x")

    [d] = result.detections
    assert d.type == "chinglish"  # 看不懂的归类，归到语义类而非硬错误
    assert d.tier == "suggestion"  # 拿不准的档位，降级为建议


async def test_grade_truncated_then_upgrades_max_tokens():
    client = _client(
        _Resp('{"detections": [', finish_reason="length"),
        _Resp(_valid_payload(), finish_reason="stop"),
    )
    result = await LLMGrader(client, model="m").grade("I is a student.")

    assert client.completions.calls[0]["max_tokens"] == 8192
    assert client.completions.calls[1]["max_tokens"] == 65536
    assert len(result.detections) == 1


async def test_grade_still_truncated_after_upgrade_raises():
    client = _client(
        _Resp("{partial", finish_reason="length"),
        _Resp("{partial", finish_reason="length"),
    )
    with pytest.raises(ToolError):
        await LLMGrader(client, model="m").grade("x")


async def test_grade_bad_json_retries_once_then_succeeds():
    client = _client(_Resp("{not valid json"), _Resp(_valid_payload()))
    result = await LLMGrader(client, model="m").grade("I is a student.")

    assert len(client.completions.calls) == 2
    assert len(result.detections) == 1


async def test_grade_bad_json_always_raises_tool_error():
    client = _client(_Resp("{bad"), _Resp("{bad again"))
    with pytest.raises(ToolError):
        await LLMGrader(client, model="m").grade("x")


async def test_grade_strips_markdown_code_fence():
    fenced = "```json\n" + _valid_payload() + "\n```"
    result = await LLMGrader(_client(_Resp(fenced)), model="m").grade("I is.")
    assert len(result.detections) == 1


async def test_grade_json_mode_unsupported_falls_back():
    client = _client(
        _FakeHTTPError(400, "response_format json_object is not supported"),
        _Resp(_valid_payload()),
    )
    result = await LLMGrader(client, model="m").grade("I is.")

    assert "response_format" in client.completions.calls[0]
    assert "response_format" not in client.completions.calls[1]
    assert len(result.detections) == 1


async def test_grade_retryable_500_retries_then_succeeds():
    client = _client(_FakeHTTPError(500), _Resp(_valid_payload()))
    grader = LLMGrader(client, model="m", backoff_base=0)
    result = await grader.grade("I is.")

    assert len(client.completions.calls) == 2
    assert len(result.detections) == 1


async def test_grade_4xx_does_not_retry():
    client = _client(_FakeHTTPError(401, "unauthorized"))
    with pytest.raises(ToolError):
        await LLMGrader(client, model="m").grade("x")
    assert len(client.completions.calls) == 1


async def test_grade_clean_text_empty_detections():
    payload = json.dumps(
        {"detections": [], "summary": "写得很地道，没有明显问题"}, ensure_ascii=False
    )
    result = await LLMGrader(_client(_Resp(payload)), model="m").grade(
        "If I were rich, I would travel the world."
    )
    assert result.detections == []
    assert "没有明显问题" in result.summary


async def test_grade_skips_entries_with_empty_original():
    payload = json.dumps(
        {
            "detections": [
                {"original": "", "corrected": "y"},
                {
                    "original": "I is",
                    "corrected": "I am",
                    "type": "hard",
                    "tier": "error",
                    "explanation_zh": "主谓不一致",
                },
            ],
            "summary": "",
        },
        ensure_ascii=False,
    )
    result = await LLMGrader(_client(_Resp(payload)), model="m").grade("I is.")
    assert [d.original for d in result.detections] == ["I is"]


# --- 5b：批改侧产出知识点代码 error_code（D17：仅最终 hard+error 且白名单内才追踪）---


def _grade_json(item: dict) -> str:
    return json.dumps({"detections": [item], "summary": ""}, ensure_ascii=False)


def test_parse_hard_error_gets_whitelisted_code_case_insensitive():
    content = _grade_json(
        {
            "original": "I go yesterday",
            "corrected": "I went yesterday",
            "type": "hard",
            "tier": "error",
            "explanation_zh": "过去时间要用过去式",
            "error_code": "verb:tense",  # 模型给小写，规范化到标准码
        }
    )
    [d] = parse_grade_content(content).detections
    assert d.error_code == "VERB:TENSE"


def test_parse_hard_error_out_of_scope_code_is_dropped():
    # 介词 PREP 是硬错但不在第一版 6 类：照常批改，代码留空、不追踪
    content = _grade_json(
        {
            "original": "good in",
            "corrected": "good at",
            "type": "hard",
            "tier": "error",
            "explanation_zh": "擅长某事是 be good at",
            "error_code": "PREP",
        }
    )
    [d] = parse_grade_content(content).detections
    assert d.type == "hard" and d.tier == "error"  # 硬错本身保留
    assert d.error_code is None  # 只是不进掌握度账本


@pytest.mark.parametrize(
    ("issue_type", "tier"),
    [("chinglish", "error"), ("hard", "suggestion"), ("wordy", "suggestion")],
)
def test_parse_non_trackable_detection_ignores_any_code(issue_type, tier):
    # 中式英语 / 啰嗦 / 可改可不改的建议，即使塞了合法代码也不追踪
    content = _grade_json(
        {
            "original": "very big",
            "corrected": "enormous",
            "type": issue_type,
            "tier": tier,
            "explanation_zh": "可更地道",
            "error_code": "DET",
        }
    )
    [d] = parse_grade_content(content).detections
    assert d.error_code is None


def test_parse_hard_error_without_code_field_is_none():
    content = _grade_json(
        {
            "original": "I is",
            "corrected": "I am",
            "type": "hard",
            "tier": "error",
            "explanation_zh": "主谓一致",
        }
    )
    [d] = parse_grade_content(content).detections
    assert d.error_code is None


def test_system_prompt_contract_lists_every_whitelisted_code():
    # 护栏：白名单扩到第 7 类时，这个测试会提醒同步批改提示词契约
    assert "error_code" in SYSTEM_PROMPT
    for code in VALID_ERROR_CODES:
        assert code in SYSTEM_PROMPT


# ------------------------------------------------- 四维评分（#36）


def _scores_payload(scores):
    return json.dumps(
        {"detections": [], "summary": "总评", "scores": scores}, ensure_ascii=False
    )


def test_parse_scores_four_dimensions():
    content = _scores_payload(
        {
            "content": {"score": 72, "comment_zh": "观点清楚，第二个论点没展开"},
            "structure": {"score": 68, "comment_zh": "分段清楚，衔接词偏少"},
            "accuracy": {"score": 55, "comment_zh": "时态和主谓一致错误较多"},
            "richness": {"score": 61, "comment_zh": "词汇偏单一，句式变化少"},
        }
    )
    result = parse_grade_content(content)
    assert result.scores is not None
    assert result.scores.content.score == 72
    assert result.scores.structure.score == 68
    assert result.scores.accuracy.score == 55
    assert result.scores.richness.score == 61
    assert "论点" in result.scores.content.comment_zh


def test_parse_scores_clamps_to_0_100():
    content = _scores_payload(
        {
            "content": {"score": 130, "comment_zh": ""},
            "structure": {"score": -20, "comment_zh": ""},
            "accuracy": {"score": 88.6, "comment_zh": ""},  # 小数取整
            "richness": {"score": 90, "comment_zh": ""},
        }
    )
    scores = parse_grade_content(content).scores
    assert scores.content.score == 100
    assert scores.structure.score == 0
    assert scores.accuracy.score == 89
    assert scores.richness.score == 90


def test_parse_scores_missing_block_is_none():
    # 旧模型 / 截断没给 scores：批改本体照常，分数为 None，前端不显示分数区
    result = parse_grade_content(json.dumps({"detections": [], "summary": "好"}))
    assert result.scores is None


def test_parse_scores_allows_null_for_inapplicable_dimension():
    # 只批一句话时谈不到篇章结构：该维给 null，其余维度照常
    content = _scores_payload(
        {
            "content": {"score": 70, "comment_zh": "意思清楚"},
            "structure": {"score": None, "comment_zh": "单句不评篇章结构"},
            "accuracy": {"score": 80, "comment_zh": "基本正确"},
            "richness": {"score": 65, "comment_zh": "用词普通"},
        }
    )
    scores = parse_grade_content(content).scores
    assert scores is not None
    assert scores.structure.score is None
    assert scores.content.score == 70
    assert scores.accuracy.score == 80


def test_parse_scores_garbled_dimension_becomes_none():
    content = _scores_payload(
        {
            "content": "不是对象",
            "structure": {"score": "abc", "comment_zh": ""},
            "accuracy": {"score": 70, "comment_zh": "ok"},
            "richness": {"comment_zh": "缺分数字段"},
        }
    )
    scores = parse_grade_content(content).scores
    assert scores is not None
    assert scores.content.score is None
    assert scores.structure.score is None
    assert scores.accuracy.score == 70
    assert scores.richness.score is None


def test_parse_scores_all_null_or_invalid_is_none():
    content = _scores_payload(
        {
            "content": {"score": None},
            "structure": {"score": None},
            "accuracy": {"score": "x"},
            "richness": "bad",
        }
    )
    assert parse_grade_content(content).scores is None
