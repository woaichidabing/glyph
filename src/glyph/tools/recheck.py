"""复核（有人驳回一条 → AI 拿那一句的上下文重新判一次）的全部形状与实现。

分三层，越往上越"脏"，越往下越纯：

* **工具层** ``RecheckDetectionTool`` —— 注册名 ``recheck_detection``，按设计文档
  §1.9 的 R1–R7 编排（校验 → 切上下文 → 调复核器 → 归一化 → 组装出参）；
* **模型层** ``LLMRechecker`` —— 非流式调模型、JSON 容错解析、有限重试（可注入，
  由 ``Rechecker`` 协议收口，测试换假实现即可离线跑）；
* **纯函数层** ``normalize_verdict()`` —— 按 §1.7 的 N-1~N-7 把模型回答归一化成三态。

归一化里最要紧的两条**方向性**规则：

1. **N-1 拿不准就撤，不维持**（``status`` 非法/缺失 → ``withdrawn``）。误报伤害大于
   漏判——AI 自己都说不清的时候，不该让人为同一个疑似误判再看一遍。
2. **N-6 复核不是重扫**（夹带的别处条目一律丢弃）。否则驳回 1 条会冒出 3 条，
   后置裁决的确定性当场崩掉。

上下文切分在 ``context.py``。

取消信号为什么安全：``CancelledError`` 在 Python 3.8+ 继承自 ``BaseException``
而非 ``Exception``，任何 ``except Exception`` 都天然不会误捕。只需做到两点——
① 不写 ``except BaseException``；② 不额外套 ``asyncio.shield`` / ``asyncio.timeout``
（会与 loop 的 ``wait_for`` 抢因果）。本文件两处 ``await`` 都遵守这两条。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, get_args

from ..retry import is_retryable
from ..runtime.errors import ToolError
from . import prompts
from .base import ReadOnlyTool
from .context import locate_context

# 复用 grader.py 里已经跑顺的那套「JSON 容错解析 + json_mode 不支持探测」：
# 复核与批改是同构的一次性 JSON 调用，两处行为必须一致，否则会出现
# 「批改能自愈、复核不能」这种说不通的怪事（R1 单点原则的另一面）。
from .grader import _is_json_mode_unsupported, _loads_content  # noqa: PLC2701
from .grading_models import (
    ContextScope,
    Detection,
    IssueType,
    RecheckInfo,
    RecheckStatus,
    Tier,
    effective_error_code,
)

_VALID_STATUS: frozenset[str] = frozenset(get_args(RecheckStatus))
_VALID_TIERS: frozenset[str] = frozenset(get_args(Tier))
_VALID_TYPES: frozenset[str] = frozenset(get_args(IssueType))

# N-7：三态固定文案，写死在这里，不依赖模型输出——UI 永远有可读文案
NOTE_TEMPLATES: dict[str, str] = {
    "upheld": "已复核第 {round} 次：AI 仍认为这里有需要修改的地方。"
    "你可以再次驳回、采纳，或自己改。",
    "withdrawn": "已复核第 {round} 次：AI 承认这条判断有误，本条已撤销，不会出现在批改结果里。",
    "revised": "已复核第 {round} 次：AI 换了判断，请看下面新的建议。",
}

# 模型可能夹带别处问题的字段名（N-6 负责判定后丢弃，这里只管原样收进来）
_EXTRA_LIST_KEYS: tuple[str, ...] = ("detections", "extra", "extra_raw")

# follow_up：撤销是终态，维持 / 改判都要再拍一次
_FOLLOW_UP: dict[str, str] = {
    "upheld": "pending_human",
    "revised": "pending_human",
    "withdrawn": "terminal",
}

__all__ = [
    "RecheckRequest",
    "RecheckVerdict",
    "NOTE_TEMPLATES",
    "normalize_verdict",
    "Rechecker",
    "LLMRechecker",
    "parse_recheck_content",
    "RecheckDetectionTool",
]


@dataclass(frozen=True)
class RecheckRequest:
    """复核请求：只判这一处，不看别处。"""

    detection: Detection
    context_text: str
    scope: ContextScope
    round: int
    note: str = ""


@dataclass(frozen=True)
class RecheckVerdict:
    """复核器的原始结论（归一化之前，允许脏数据）。"""

    status: RecheckStatus
    reason_zh: str = ""
    corrected: str | None = None
    explanation_zh: str | None = None
    tier: str | None = None
    type: str | None = None
    # 模型可能夹带的额外条目原文（N-6 要按"是否与本条目相交"判定后丢弃）
    extra_raw: list[dict] = field(default_factory=list)


def _clean(value: str | None) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _overlaps(det: Detection, item: dict[str, Any]) -> bool:
    """夹带的条目是否落在**被复核的这一处**上。

    给区间就比区间；给文本就判断与被复核片段是否互相包含。两者都没有、
    或被复核条目自己没有位置与原文时，一律视为**不相交**（保守丢弃）。
    """
    e_start, e_end = item.get("start"), item.get("end")
    if isinstance(e_start, int) and isinstance(e_end, int):
        d_start, d_end = det.start, det.end
        if isinstance(d_start, int) and isinstance(d_end, int):
            return not (e_start >= d_end or e_end <= d_start)
        return False

    original = item.get("original")
    if isinstance(original, str) and original and det.original:
        return original in det.original or det.original in original

    return False


def normalize_verdict(
    verdict: RecheckVerdict,
    detection: Detection,
    round_: int,
    scope: ContextScope,
) -> tuple[RecheckInfo, list[str]]:
    """把复核器的原始结论归一化成 ``RecheckInfo``，返回 ``(结论, 提示列表)``。

    规则按执行顺序，全部确定性、离线可测（N-1 ~ N-7）。
    """
    warnings: list[str] = []

    # N-1 拿不准就撤，不维持
    status = verdict.status if verdict.status in _VALID_STATUS else "withdrawn"
    if verdict.status not in _VALID_STATUS:
        warnings.append("复核结论无法识别，按『撤销』处理，不会出现在批改结果里")

    # N-2 说"改判"却没有任何实质变化 = 模型在说空话 → 按维持处理
    if status == "revised":
        new_four = (
            verdict.corrected,
            verdict.tier,
            verdict.type,
            verdict.explanation_zh,
        )
        old_four = (
            detection.corrected,
            detection.tier,
            detection.type,
            detection.explanation_zh,
        )
        same = all(
            (new is None) or (new == old) for new, old in zip(new_four, old_four, strict=False)
        )
        if same:
            status = "upheld"
            warnings.append("复核说要改判但与原判断没有差别，按『维持原判』处理")

    # N-6 局部性硬约束：夹带的别处条目全部丢弃
    extra = verdict.extra_raw or []
    if extra:
        dropped = [item for item in extra if not _overlaps(detection, item)]
        if dropped:
            warnings.append(f"复核返回了 {len(dropped)} 条本条目之外的问题，已忽略")

    if status == "withdrawn":
        # N-5 撤销 = 不再携带任何修改建议
        corrected = explanation = tier = type_ = None
    elif status == "upheld":
        # N-4 维持原判 = 原样维持，不允许顺手改定性；模型的新理由只落到 reason_zh
        corrected = detection.corrected
        explanation = detection.explanation_zh
        tier = detection.tier
        type_ = detection.type
    else:
        # N-3 改判：空值沿用原值，非法枚举沿用原值
        corrected = _clean(verdict.corrected) or detection.corrected
        explanation = _clean(verdict.explanation_zh) or detection.explanation_zh
        tier = verdict.tier if verdict.tier in _VALID_TIERS else detection.tier
        type_ = verdict.type if verdict.type in _VALID_TYPES else detection.type

    return (
        RecheckInfo(
            status=status,  # type: ignore[arg-type] - 已归一到三态之内
            round=round_,
            scope=scope,
            corrected=corrected,
            explanation_zh=explanation,
            tier=tier,
            type=type_,
            note_zh=NOTE_TEMPLATES[status].format(round=round_),
            reason_zh=verdict.reason_zh or "",
        ),
        warnings,
    )


class Rechecker(Protocol):
    """复核器协议：只判这一处，不看别处。

    失败抛 ``ToolError``（由主模型翻译成人话）；``CancelledError`` 原样传播——
    实现里不得用 ``except BaseException`` 兜住它。
    """

    async def review(self, req: RecheckRequest) -> RecheckVerdict: ...


def _text(value: Any) -> str | None:
    """模型给的字段一律按"非空字符串"收；空串与非字符串都当没给。"""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _collect_extra(data: dict[str, Any]) -> list[dict[str, Any]]:
    """把模型夹带的数组**原样**收进来，判定（N-6 丢弃）交给 ``normalize_verdict``。"""
    extra: list[dict[str, Any]] = []
    for key in _EXTRA_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            extra.extend(item for item in value if isinstance(item, dict))
    return extra


def parse_recheck_content(content: str) -> RecheckVerdict:
    """把模型文本响应解析成 ``RecheckVerdict``（**不做归一化**，脏数据原样保留）。

    ``status`` 非法或缺失时**不抛错**：原样塞进 ``RecheckVerdict``，由
    ``normalize_verdict`` 的 N-1 兜底成 ``withdrawn``。这一步抛错方向就反了——
    模型说不清的时候，正确做法是撤掉这条，而不是让整次复核失败。
    """
    data = _loads_content(content)
    return RecheckVerdict(
        status=str(data.get("status") or ""),  # type: ignore[arg-type] - 非法值交给 N-1
        reason_zh=str(data.get("reason_zh") or ""),
        corrected=_text(data.get("corrected")),
        explanation_zh=_text(data.get("explanation_zh")),
        tier=_text(data.get("tier")),
        type=_text(data.get("type")),
        extra_raw=_collect_extra(data),
    )


class LLMRechecker:
    """调用 OpenAI 兼容 /chat/completions（非流式）拿复核结论。

    与 ``LLMGrader`` 同构（同一 client、同一 ``grade_model``）：json_mode 不支持时
    关掉重试、finish=length 升档重试、解析失败重试、可重试判定走 ``retry.is_retryable``。
    复核是**短输出**，因此默认 max_tokens 比批改小一个量级。
    """

    def __init__(
        self,
        client,
        model: str,
        *,
        max_tokens: int = 1024,
        max_tokens_upgrade: int = 8192,
        max_parse_retries: int = 1,
        max_network_retries: int = 3,
        backoff_base: float = 0.5,
        temperature: float = 0.1,
    ) -> None:
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self.max_tokens_upgrade = max_tokens_upgrade
        self.max_parse_retries = max_parse_retries
        self.max_network_retries = max_network_retries
        self.backoff_base = backoff_base
        self.temperature = temperature

    async def review(self, req: RecheckRequest) -> RecheckVerdict:
        messages = prompts.build_recheck_messages(req)
        max_tokens = self.max_tokens
        json_mode = True
        parse_retries = 0
        network_attempt = 0

        while True:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": self.temperature,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                response = await self._client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - 统一归类为 ToolError
                # except Exception 天然不捕 CancelledError（BaseException 子类），
                # 取消信号原样冒出，主循环的协作式取消不会被吞。
                if json_mode and _is_json_mode_unsupported(exc):
                    json_mode = False
                    continue
                if is_retryable(exc) and network_attempt < self.max_network_retries:
                    await asyncio.sleep(self.backoff_base * (2**network_attempt))
                    network_attempt += 1
                    continue
                raise ToolError(f"复核模型调用失败：{exc}") from exc

            choice = response.choices[0]
            content = getattr(choice.message, "content", None) or ""
            finish_reason = getattr(choice, "finish_reason", None)

            if finish_reason == "length":
                if max_tokens < self.max_tokens_upgrade:
                    max_tokens = self.max_tokens_upgrade  # 静默升档后重试
                    continue
                raise ToolError("复核结果过长且已达输出上限，请把文本缩短后重试")

            try:
                return parse_recheck_content(content)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                if parse_retries < self.max_parse_retries:
                    parse_retries += 1
                    continue
                raise ToolError(f"复核模型返回的不是合法 JSON，重试后仍失败：{exc}") from exc


class RecheckDetectionTool(ReadOnlyTool):
    """``recheck_detection``：有人驳回某条时调用，**只复核这一处**。

    无状态、只读、跨调用无记忆：单条次数靠入参 ``round`` 兜底（R2），
    单篇总量由装配层 ``web/`` 独占负责（设计稿 C-3）。
    """

    name = "recheck_detection"
    description = (
        "复核一条被驳回的批改建议：只针对这一处，取它所在的句子（必要时整段）"
        "作为上下文重新判断，给出维持原判 / 撤销 / 改判三种结论之一。"
        "当且仅当有人对某一条批改结果点『驳回』并要求 AI 重新判断时调用。"
        "不要用于：用户没提异议就主动复核整篇，或拿它代替 grade_text 批改全文。"
        "它给出的结论**仍是候选**：如实转达，不要替用户下结论；"
        "用户提异议不等于你原来错了——复核是独立再判一次，**不是让你改口**。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "description": "已确认的英文全文（唯一真相源，不是未确认的导入文本）",
            },
            "detection": {
                "type": "object",
                "description": "被驳回条目的完整对象，原样回带",
                "properties": {
                    "id": {"type": "string"},
                    "original": {"type": "string"},
                    "corrected": {"type": "string"},
                    "type": {"type": "string", "enum": ["hard", "chinglish", "wordy"]},
                    "tier": {"type": "string", "enum": ["error", "suggestion"]},
                    "source": {"type": "string", "enum": ["model", "spell"]},
                    "start": {"type": ["integer", "null"]},
                    "end": {"type": ["integer", "null"]},
                    "aligned": {"type": "boolean"},
                    "explanation_zh": {"type": "string"},
                    "error_code": {
                        "type": ["string", "null"],
                        "description": (
                            "知识点代码：grade 结果里这条带 error_code 就原样回带，没有就 null；"
                            "复核后会按最终裁决重新确认它是否仍有效。"
                        ),
                    },
                },
                "required": ["id", "original", "source"],
                "additionalProperties": False,
            },
            "round": {
                "type": "integer",
                "minimum": 1,
                "description": "第几轮复核，从 1 开始",
            },
            "note": {
                "type": "string",
                "description": "驳回时附的自由文本备注（可选，不结构化）",
            },
        },
        "required": ["text", "detection", "round"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        rechecker: Rechecker,
        max_rounds: int,
        min_context_words: int = 8,
        max_context_chars: int = 4096,
    ) -> None:
        self._rechecker = rechecker
        self._max_rounds = max_rounds
        self._min_context_words = min_context_words
        self._max_context_chars = max_context_chars

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        """按设计文档 §1.9 的 R1–R7 走一遍，返回 §1.6 的出参字段表。"""
        # R1 取参
        text = str(args.get("text") or "")
        detection_arg = args.get("detection") or {}
        round_ = args.get("round") or 0
        note = str(args.get("note") or "")

        if not text.strip():
            raise ToolError("待复核的文本为空，无法复核")

        # R2 次数兜底（守 R5；只兜单条，单篇总量由装配层拦）
        if round_ > self._max_rounds:
            raise ToolError(
                f"这条已经复核过 {self._max_rounds} 次，次数已用完。"
                "可以直接采纳、自己改，或放弃这条。"
            )

        # R3 宽松重建：缺字段按 Detection 的默认值补，只卡 id 与 original 非空
        det = Detection.model_validate(detection_arg)
        if not det.id or not det.original:
            raise ToolError("缺少要复核的条目编号或原文片段，无法复核")

        # R4 切上下文（start 为 None 时内部自动升段落）
        warnings: list[str] = []
        context_text, scope = locate_context(
            text,
            det.start,
            det.end,
            original=det.original,
            min_words=self._min_context_words,
            max_chars=self._max_context_chars,
            warnings=warnings,
        )

        # R5 调复核器：ToolError 与 CancelledError 都**原样穿透**（这里不写 try/except，
        # 是最稳的写法——写了反而容易顺手捕错）。
        request = RecheckRequest(det, context_text, scope, round_, note)
        verdict = await self._rechecker.review(request)

        # R6 归一化（N-1~N-7）
        info, verdict_warnings = normalize_verdict(verdict, det, round_, scope)
        warnings.extend(verdict_warnings)

        # R7 组装出参：顶层字段与 detection.recheck 同源（§1.6），由测试断言逐字段相等
        new_detection = det.model_copy(update={"recheck": info})
        detection_payload = new_detection.model_dump()
        # D17：出口给模型的 error_code 必须是"最终有效码"——撤销 / 改判为非硬错即清空，
        # 维持或改判仍是硬错才沿用原码。否则模型会拿着已失效的码去 remember 建账。
        detection_payload["error_code"] = effective_error_code(new_detection)
        return {
            "id": det.id,
            "detection": detection_payload,
            "status": info.status,
            "round": info.round,
            "scope": info.scope,
            "corrected": info.corrected,
            "explanation_zh": info.explanation_zh,
            "tier": info.tier,
            "type": info.type,
            "note_zh": info.note_zh,
            "reason_zh": info.reason_zh,
            "follow_up": _FOLLOW_UP[info.status],
            "warnings": warnings,
        }
