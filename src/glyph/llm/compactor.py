"""L3 上下文压缩的"摘要写手"客户端（方案 §9.2 / §9.3 / §10，v2.1 #65 切片6）。

与主脑（``llm/qwen.py`` 走 Responses / 流式 / 带工具）刻意分开：摘要只要一次性结构化
文本，不要流式、不要工具，因此走候选模型最广、最便宜的 **chat/completions** 通道，
默认写手 ``qwen3.7-flash``（§18 探针：标称 1M 窗口、教学金标 9/9、temp=0 稳定、一次约
¥0.1–0.2）。

失败分级（§10）：
- 网络抖动 / 5xx / 429：有限次指数退避重试（复用全项目单点 ``retry.is_retryable``）；
- **输入超长**（HTTP 413，或 400 体内含 context_length_exceeded / maximum context）：
  重试无意义，识别为 :class:`ContextTooLongError`，上层把材料**对半砍（留更新半）**
  后整体重试一次；连半份都超长才放弃；
- 输出被截断（finish_reason=length）或空摘要：同样减半材料重试一次（材料越短、写手
  越从容，也顺带排除了输入侧的隐性超限）；仍失败按不可重试错误抛出；
- ``CancelledError`` 永远原样向上抛，绝不吞掉取消语义。

写手只接收 plan 备好的材料记录与上一条摘要种子（B2：session 不 import llm，写手由
loop 在 #66 持有并调用）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass

import openai

from ..config import Settings, load_settings
from ..retry import is_context_too_long, is_quota_or_auth_error, is_retryable
from ..session.compaction_l3 import split_turns
from ..session.records import (
    AssistantMessagePayload,
    FinalizePayload,
    Record,
    RecordType,
    ToolCallPayload,
    ToolResultPayload,
    UserMessagePayload,
)
from .base import ContextTooLongError, LLMError

# 金标验证过的固定结构（§18 探针，qwen3.7-flash 9/9 要点全中，temperature=0）。
COMPACT_SYSTEM_PROMPT = (
    "你是对话压缩器。把下面这段英语学习助手的多轮会话浓缩成结构化中文摘要，供后续继续辅导。"
    "只依据原文、不编造；必须用以下固定小节，每节简明：\n"
    "## 用户目标与偏好\n## 已定稿成果（逐条含 grade 代号、篇名、错误数、定稿结论）\n"
    "## 进行中 / 未决\n## 关键事实（查过的知识点、用户水平、考试目标）\n"
    "## 不能丢的细节"
)

_SEED_HEADER = (
    "【这是上一次压缩留下的摘要。请在它的基础上滚动更新：仍有效的信息保留，与下面新对话"
    "冲突的以新对话为准，尤其不要丢失其中已经定稿的成果。】"
)
_MATERIAL_HEADER = "【下面是这次需要并入摘要的更早对话】"
_TRUNCATED_NOTE = (
    "（说明：还有更旧的一小段对话因超出摘要窗口未能附上、已省略；摘要无需提及它们，"
    "也不要编造其内容。）"
)

# 非批改工具结果在喂给写手时最多保留的 JSON 字符数（避免把大段检索原文堆进摘要材料）。
_TOOL_RESULT_PREVIEW = 300


@dataclass(frozen=True)
class SummaryResult:
    """一次成功摘要的结果（交给 loop 落账 + 记账）。"""

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    material_halved: bool
    finish_reason: str


# 上下文超长异常 / 归类的单点定义在 llm.base / retry，这里 re-export 供写手侧与既有引用使用。
__all__ = [
    "ContextTooLongError",
    "SummaryResult",
    "CompactionSummarizer",
    "halve_material_records",
    "is_context_too_long",
    "render_material_transcript",
]

# ---------- 材料 → 写手可读文本（纯函数） ----------


def _render_grade_result(call_id: str, output: dict | None) -> str:
    output = output or {}
    lines = [f"[批改结果·grade@{call_id}] {output.get('summary', '')}".rstrip()]
    detections = output.get("detections")
    if isinstance(detections, list):
        for det in detections:
            if not isinstance(det, dict):
                continue
            kind = det.get("tier") or det.get("type") or ""
            lines.append(
                f"  - {det.get('original', '')} → {det.get('corrected', '')}"
                + (f"（{kind}）" if kind else "")
            )
    return "\n".join(lines)


def _render_record(record: Record) -> str | None:
    """把一条流水记录转成写手材料里的一行 / 一段；元记录返回 None（跳过）。"""
    rtype = record.record_type
    if rtype is RecordType.USER_MESSAGE:
        return f"[用户] {record.payload_as(UserMessagePayload).text}"
    if rtype is RecordType.ASSISTANT_MESSAGE:
        text = record.payload_as(AssistantMessagePayload).text
        return f"[助手] {text}" if text else None
    if rtype is RecordType.TOOL_CALL:
        call = record.payload_as(ToolCallPayload)
        if call.name == "grade_text":
            # grade_text 的入参 text 就是学习者提交的英文原文，是错误点 / 定稿的源头，必须保留。
            original = str(call.arguments.get("text") or "")
            return f"[用户提交的英文原文·grade@{call.id}]\n{original}"
        query = call.arguments.get("query") or call.arguments.get("text")
        suffix = f"：{query}" if query else ""
        return f"[助手调用工具 {call.name}{suffix}]"
    if rtype is RecordType.TOOL_RESULT:
        result = record.payload_as(ToolResultPayload)
        if not result.ok:
            return f"[工具 {result.name} 失败] {result.error or ''}".rstrip()
        if result.name == "grade_text":
            return _render_grade_result(result.call_id, result.output)
        blob = json.dumps(result.output, ensure_ascii=False) if result.output else ""
        return f"[工具 {result.name} 结果] {blob[:_TOOL_RESULT_PREVIEW]}"
    if rtype is RecordType.FINALIZE:
        finalize = record.payload_as(FinalizePayload)
        body = finalize.final_text or finalize.comment or "（已定稿，定稿文本未记录）"
        return f"[已定稿·grade@{finalize.grade_call_id}]\n{body}"
    # reasoning / turn_end / cancel / compact_summary / compact_boundary / session_meta：
    # 摘要不喂摘要自己、也不喂过程性思考与计数元数据。
    return None


def render_material_transcript(
    records: Sequence[Record],
    *,
    seed_summary_text: str | None = None,
    material_truncated: bool = False,
) -> str:
    """把选中的旧往来记录（旧→新）渲染成喂给写手的材料文本。

    二次压缩时 ``seed_summary_text`` 带上一条摘要（滚动更新种子）；``material_truncated``
    表示最旧一段已因写手窗口装不下而被省略，需显式告诉写手不要编造那部分。
    """
    parts: list[str] = []
    if seed_summary_text and seed_summary_text.strip():
        parts.extend([_SEED_HEADER, seed_summary_text.strip(), "", _MATERIAL_HEADER])
    if material_truncated:
        parts.append(_TRUNCATED_NOTE)
    for record in records:
        rendered = _render_record(record)
        if rendered:
            parts.append(rendered)
    return "\n".join(parts)


def halve_material_records(records: Sequence[Record]) -> tuple[Record, ...]:
    """材料对半砍、**永远留更新的一半**（§9.3），尽量按完整往返边界切，不拆散一轮对话。

    连一个完整往返都凑不齐时退化为按原始记录保留后半段；材料已空则返回空。
    """
    records = list(records)
    if not records:
        return ()
    turns = split_turns(records)
    if len(turns) <= 1:
        mid = len(records) // 2
        return tuple(records[mid:])
    keep = turns[len(turns) // 2 :]
    out: list[Record] = []
    for turn in keep:
        out.extend(turn.records)
    return tuple(out)


# ---------- 写手客户端 ----------


class CompactionSummarizer:
    """调 chat/completions 产出 L3 摘要；封装网络退避与"超长 / 截断减半一次"的后手。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client=None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self._settings = settings or load_settings()
        self.model = self._settings.compact_model
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._client = client or openai.AsyncOpenAI(
            api_key=self._settings.api_key or "missing-api-key",
            base_url=self._settings.base_url,
        )

    def _payload(self, transcript: str) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
                {"role": "user", "content": "请压缩下面这段英语学习会话：\n\n" + transcript},
            ],
            # 摘要输出预留（config 默认 8k）：截断时的判据也用它。
            "max_tokens": self._settings.compact_output_reserve,
            "temperature": 0,
        }

    async def _create_with_net_retry(self, payload: dict):
        """单次 chat 调用：网络抖动有限退避；输入超长立即转 ContextTooLongError。"""
        attempt = 0
        while True:
            try:
                return await self._client.chat.completions.create(**payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一归类
                if is_context_too_long(exc):
                    raise ContextTooLongError(str(exc)) from exc
                if is_retryable(exc) and attempt < self.max_retries:
                    await asyncio.sleep(self.backoff_base * (2**attempt))
                    attempt += 1
                    continue
                raise LLMError(
                    str(exc),
                    retryable=is_retryable(exc),
                    quota_or_auth=is_quota_or_auth_error(exc),
                ) from exc

    async def complete(self, messages: list[dict], **kwargs) -> str:
        """通用一问一答：给记忆提炼 / 整理这类"要一段 JSON 回来"的活复用。

        存在的意义是**不新增模型、不新增 client**：提炼与整理都用这个便宜写手
        （settings.compact_model），与压缩共用同一把钥匙、同一套网络重试。
        """
        payload = {
            "model": kwargs.pop("model", self.model),
            "messages": messages,
            "temperature": 0,
        }
        payload.update(kwargs)
        response = await self._create_with_net_retry(payload)
        choice = response.choices[0]
        return (getattr(choice.message, "content", None) or "").strip()

    async def summarize(
        self,
        *,
        material_records: Sequence[Record],
        seed_summary_text: str | None = None,
        material_truncated: bool = False,
    ) -> SummaryResult:
        """产出摘要。材料超长 / 输出截断 / 空摘要时，自动减半材料（留更新半）重试一次。"""
        records = list(material_records)
        halved = False
        attempts = 0

        while True:
            transcript = render_material_transcript(
                records,
                seed_summary_text=seed_summary_text,
                material_truncated=material_truncated or halved,
            )
            try:
                response = await self._create_with_net_retry(self._payload(transcript))
            except ContextTooLongError:
                records, halved, retry = self._maybe_halve(records, halved, attempts)
                if retry:
                    attempts += 1
                    continue
                raise

            choice = response.choices[0]
            text = (getattr(choice.message, "content", None) or "").strip()
            finish = getattr(choice, "finish_reason", None) or "stop"

            if not text or finish == "length":
                records, halved, retry = self._maybe_halve(records, halved, attempts)
                if retry:
                    attempts += 1
                    continue
                raise LLMError(
                    f"摘要写手连续两次未产出完整摘要（finish_reason={finish}，"
                    f"空摘要={not text}）；本轮放弃压缩、全量继续",
                    retryable=False,
                )

            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0
            return SummaryResult(
                text=text,
                model=self.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                material_halved=halved,
                finish_reason=finish,
            )

    @staticmethod
    def _maybe_halve(
        records: list[Record], halved: bool, attempts: int
    ) -> tuple[list[Record], bool, bool]:
        """决定是否还能减半重试：只允许一次；返回 (新记录, 是否减半过, 是否继续重试)。"""
        if attempts >= 1 or not records:
            return records, halved, False
        return list(halve_material_records(records)), True, True
