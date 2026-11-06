"""L3 摘要写手 client（方案 §9.2 / §9.3 / §10，v2.1 #65 切片6）——不联网单测。

钉四块：
1. 材料转写保住教学要点（用户原文 / 批改错误点 / 定稿全文 / 目标偏好），二次压缩带种子；
2. 材料对半砍永远"留更新半"、按往返边界不切断；
3. 413 / context_length_exceeded 被识别为"输入超长"（区别于网络抖动），触发减半而非退避；
4. summarize：成功、超长减半重试、length / 空摘要减半、连续失败报错、网络错误退避、
   CancelledError 不被吞。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from glyph.llm.base import LLMError
from glyph.llm.compactor import (
    CompactionSummarizer,
    ContextTooLongError,
    halve_material_records,
    is_context_too_long,
    render_material_transcript,
)
from glyph.session.records import (
    AssistantMessagePayload,
    FinalizePayload,
    ToolCallPayload,
    ToolResultPayload,
    UserMessagePayload,
    build_record,
)


def rec(seq: int, payload):
    return build_record(seq=seq, ts=f"t-{seq}", payload=payload)


def _resp(content: str, *, finish: str = "stop", pin=100, pout=50):
    return NS(
        choices=[NS(message=NS(content=content), finish_reason=finish)],
        usage=NS(prompt_tokens=pin, completion_tokens=pout),
    )


class _ScriptedCompletions:
    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    async def create(self, **payload):
        self.calls.append(payload)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, script):
        self.completions = _ScriptedCompletions(script)

    @property
    def chat(self):
        return NS(completions=self.completions)


class _Exc(Exception):
    def __init__(self, status_code, text="something went wrong"):
        self.status_code = status_code
        self._text = text
        super().__init__(text)

    def __str__(self):
        return self._text


# ---------- 材料转写 ----------


def _teaching_records():
    return [
        rec(0, UserMessagePayload(text="帮我改这篇：Yesterday I go to park and see three dog.")),
        rec(
            1,
            ToolCallPayload(
                id="g1",
                name="grade_text",
                arguments={"text": "Yesterday I go to park and see three dog."},
            ),
        ),
        rec(
            2,
            ToolResultPayload(
                call_id="g1",
                name="grade_text",
                ok=True,
                output={
                    "summary": "时态 + 复数共 3 处",
                    "detections": [
                        {"original": "go", "corrected": "went", "tier": "error"},
                        {"original": "dog", "corrected": "dogs", "tier": "error"},
                    ],
                },
            ),
        ),
        rec(
            3,
            FinalizePayload(
                grade_call_id="g1",
                final_text="Yesterday I went to the park and saw three dogs.",
            ),
        ),
        rec(4, UserMessagePayload(text="我下个月考雅思写作，目标 6.5，讲语法结合雅思。")),
    ]


def test_transcript_keeps_original_detections_final_and_goal() -> None:
    text = render_material_transcript(_teaching_records())

    assert "Yesterday I go to park" in text  # 用户原文
    assert "go" in text and "went" in text  # 错误点与纠正
    assert "dog" in text and "dogs" in text
    assert "Yesterday I went to the park and saw three dogs." in text  # 定稿全文
    assert "雅思" in text and "6.5" in text  # 目标偏好


def test_transcript_with_seed_and_truncation_notes() -> None:
    text = render_material_transcript(
        _teaching_records(),
        seed_summary_text="旧摘要：学习者在练一般过去时。",
        material_truncated=True,
    )
    assert "旧摘要：学习者在练一般过去时。" in text
    assert "滚动更新" in text
    assert "省略" in text


# ---------- 对半砍 ----------


def test_halve_keeps_newer_half_and_whole_turns() -> None:
    records = []
    seq = 0
    for i in range(6):
        records.append(rec(seq, UserMessagePayload(text=f"old-marker-{i}")))
        seq += 1
        records.append(rec(seq, AssistantMessagePayload(text=f"reply-{i}")))
        seq += 1

    kept = halve_material_records(records)
    text = render_material_transcript(kept)

    assert "old-marker-5" in text  # 最新半保留
    assert "old-marker-0" not in text  # 最旧半砍掉
    assert "old-marker-2" not in text
    assert "old-marker-3" in text
    # 按往返边界：留下 3 个完整 turn（user + assistant 各 3，共 6 条）。
    assert len(kept) == 6


# ---------- 超长识别 ----------


def test_is_context_too_long_classification() -> None:
    assert is_context_too_long(_Exc(413, "payload too large"))
    assert is_context_too_long(_Exc(400, "This model's maximum context length is 4096 tokens"))
    assert is_context_too_long(_Exc(400, "context_length_exceeded: please reduce your prompt"))
    # 网络抖动 / 限流不算超长。
    assert not is_context_too_long(_Exc(429, "rate limit"))
    assert not is_context_too_long(_Exc(500, "server error"))
    assert not is_context_too_long(_Exc(401, "unauthorized"))


# ---------- summarize 主流程 ----------


def _material():
    records = []
    seq = 0
    for i in range(6):
        records.append(rec(seq, UserMessagePayload(text=f"mat-marker-{i} " + "y" * 200)))
        seq += 1
        records.append(rec(seq, AssistantMessagePayload(text=f"r-{i}")))
        seq += 1
    return records


@pytest.mark.asyncio
async def test_summarize_success_uses_chat_shape() -> None:
    client = _FakeClient([_resp("## 用户目标与偏好\n...", pin=120, pout=40)])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    result = await summarizer.summarize(material_records=_material())

    assert result.text.startswith("## 用户目标与偏好")
    assert result.material_halved is False
    assert result.input_tokens == 120 and result.output_tokens == 40
    assert result.model == summarizer.model
    (call,) = client.completions.calls
    assert call["model"] == summarizer.model
    assert call["temperature"] == 0
    assert call["max_tokens"] > 0
    assert call["messages"][0]["role"] == "system"
    assert "mat-marker-5" in call["messages"][1]["content"]


@pytest.mark.asyncio
async def test_summarize_halves_on_context_too_long_then_succeeds() -> None:
    client = _FakeClient([_Exc(400, "context_length_exceeded"), _resp("减半后成功的摘要")])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    result = await summarizer.summarize(material_records=_material())

    assert result.material_halved is True
    assert result.text == "减半后成功的摘要"
    assert len(client.completions.calls) == 2
    first, second = client.completions.calls
    assert len(second["messages"][1]["content"]) < len(first["messages"][1]["content"])
    assert "mat-marker-5" in second["messages"][1]["content"]
    assert "mat-marker-0" not in second["messages"][1]["content"]


@pytest.mark.asyncio
async def test_summarize_halves_on_length_finish_then_succeeds() -> None:
    client = _FakeClient([_resp("半截摘要", finish="length", pout=8000), _resp("完整摘要")])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    result = await summarizer.summarize(material_records=_material())

    assert result.material_halved is True
    assert result.text == "完整摘要"


@pytest.mark.asyncio
async def test_summarize_halves_on_empty_output_then_succeeds() -> None:
    client = _FakeClient([_resp("", finish="stop"), _resp("补出的完整摘要")])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    result = await summarizer.summarize(material_records=_material())

    assert result.material_halved is True
    assert result.text == "补出的完整摘要"


@pytest.mark.asyncio
async def test_summarize_raises_after_two_context_too_long() -> None:
    client = _FakeClient([_Exc(400, "context_length_exceeded"), _Exc(413, "too large")])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    with pytest.raises(ContextTooLongError):
        await summarizer.summarize(material_records=_material())
    assert len(client.completions.calls) == 2


@pytest.mark.asyncio
async def test_summarize_raises_after_two_incomplete_outputs() -> None:
    client = _FakeClient([_resp("半截", finish="length", pout=8000), _resp("", finish="stop")])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    with pytest.raises(LLMError) as exc_info:
        await summarizer.summarize(material_records=_material())
    assert exc_info.value.retryable is False
    assert len(client.completions.calls) == 2


@pytest.mark.asyncio
async def test_summarize_retries_transient_5xx_then_succeeds() -> None:
    client = _FakeClient([_Exc(500), _Exc(502, "bad gateway"), _resp("退避后成功")])
    summarizer = CompactionSummarizer(client=client, max_retries=3, backoff_base=0.0)

    result = await summarizer.summarize(material_records=_material())

    assert result.text == "退避后成功"
    assert result.material_halved is False
    assert len(client.completions.calls) == 3


@pytest.mark.asyncio
async def test_summarizer_does_not_swallow_cancellation() -> None:
    client = _FakeClient([asyncio.CancelledError()])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    with pytest.raises(asyncio.CancelledError):
        await summarizer.summarize(material_records=_material())
    assert len(client.completions.calls) == 1


@pytest.mark.asyncio
async def test_summarize_passes_seed_summary_to_prompt() -> None:
    client = _FakeClient([_resp("滚动后的摘要")])
    summarizer = CompactionSummarizer(client=client, backoff_base=0.0)

    await summarizer.summarize(material_records=_material(), seed_summary_text="上一轮摘要种子AAA")

    assert "上一轮摘要种子AAA" in client.completions.calls[0]["messages"][1]["content"]
