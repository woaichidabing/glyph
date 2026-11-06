"""批改模型客户端：工具内部一次性产出结构化批改 JSON（非流式）。

与主对话模型分开：批改是"长输出、重判断"的活，用较强模型一次出成品；
主对话模型只负责接待与短引导。本模块只管"拿到合法 JSON"，位置对齐、合并、
限条等纯本地处理在 merge.py。

健壮性：
- response_format=json_object 不被支持(400) 时自动关掉重试；
- finish=length 截断时升到更大 max_tokens 重试一次，仍截断则 ToolError；
- JSON 解析失败重试一次，仍坏则 ToolError；
- 连接/超时/429/5xx 有限重试，4xx 立即失败，耗尽抛 ToolError。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Protocol

from ..memory.mastery import normalize_error_code
from ..retry import is_retryable
from ..runtime.errors import ToolError
import math

from . import prompts
from .grading_models import Detection, DimensionScore, GradeResult, WritingScores

_VALID_TYPES = {"hard", "chinglish", "wordy"}
_VALID_TIERS = {"error", "suggestion"}
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
# 写作四维，顺序即前端展示顺序
_SCORE_DIMENSIONS = ("content", "structure", "accuracy", "richness")


def _coerce_score(value: object) -> int | None:
    """把模型给的分数收敛成 0-100 的整数；None / 非数 / 布尔 → None（不适用）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = float(value)
    elif isinstance(value, float):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return max(0, min(100, int(round(number))))


def _parse_scores(data: dict) -> WritingScores | None:
    """解析四维评分块；整块缺失 / 四维全空时返回 None（前端不渲染分数区）。"""
    raw = data.get("scores")
    if not isinstance(raw, dict):
        return None
    values: dict[str, DimensionScore] = {}
    for key in _SCORE_DIMENSIONS:
        node = raw.get(key)
        score: int | None = None
        comment = ""
        if isinstance(node, dict):
            score = _coerce_score(node.get("score"))
            raw_comment = node.get("comment_zh")
            if isinstance(raw_comment, str):
                comment = raw_comment.strip()
        values[key] = DimensionScore(score=score, comment_zh=comment)
    result = WritingScores(**values)
    dimensions = (result.content, result.structure, result.accuracy, result.richness)
    if all(d.score is None for d in dimensions):
        return None
    return result


class Grader(Protocol):
    """批改器协议：输入英文原文，输出模型那一路的结构化批改（未对齐位置）。"""

    async def grade(self, text: str) -> GradeResult: ...


# 错误可重试判定不在本文件，统一走 markr/retry.py::is_retryable（全项目单点，R1）


def _is_json_mode_unsupported(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400 and "response_format" in str(exc).lower()


def _loads_content(content: str) -> dict:
    """解析模型 JSON，容忍代码块包裹或前后多余文字。"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        fence = _FENCE_RE.search(content)
        if fence:
            data = json.loads(fence.group(1))
        else:
            start, end = content.find("{"), content.rfind("}")
            if start == -1 or end <= start:
                raise
            data = json.loads(content[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return data


def parse_grade_content(content: str) -> GradeResult:
    """把模型文本响应解析成 GradeResult，并对脏字段做保守兜底。"""
    data = _loads_content(content)
    raw = data.get("detections")
    if not isinstance(raw, list):
        raise ValueError("detections 字段不是数组")

    detections: list[Detection] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        original = str(item.get("original", "") or "").strip()
        if not original:  # 没有原文锚点的条目无法定位，丢弃
            continue
        issue_type = item.get("type")
        issue_type = issue_type if issue_type in _VALID_TYPES else "chinglish"
        tier = item.get("tier")
        tier = tier if tier in _VALID_TIERS else "suggestion"  # 拿不准就降级为建议
        # 知识点代码：白名单规范化后，还必须是最终 hard+error 才采信（D17）；
        # 中式英语 / 啰嗦 / 可改可不改的建议，即使模型塞了合法代码也不进账本。
        error_code = normalize_error_code(item.get("error_code"))
        if issue_type != "hard" or tier != "error":
            error_code = None
        detections.append(
            Detection(
                original=original,
                corrected=str(item.get("corrected", "") or ""),
                type=issue_type,
                tier=tier,
                explanation_zh=str(item.get("explanation_zh", "") or ""),
                source="model",
                error_code=error_code,
            )
        )
    return GradeResult(
        detections=detections,
        summary=str(data.get("summary", "") or ""),
        scores=_parse_scores(data),
    )


class LLMGrader:
    """调用 OpenAI 兼容 /chat/completions（非流式）拿批改 JSON。"""

    def __init__(
        self,
        client,
        model: str,
        *,
        max_tokens: int = 8192,
        max_tokens_upgrade: int = 65536,
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

    async def grade(self, text: str) -> GradeResult:
        messages = prompts.build_messages(text)
        max_tokens = self.max_tokens
        json_mode = True
        parse_retries = 0
        network_attempt = 0

        while True:
            payload: dict = {
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
                if json_mode and _is_json_mode_unsupported(exc):
                    json_mode = False
                    continue
                if is_retryable(exc) and network_attempt < self.max_network_retries:
                    await asyncio.sleep(self.backoff_base * (2**network_attempt))
                    network_attempt += 1
                    continue
                raise ToolError(f"批改模型调用失败：{exc}") from exc

            choice = response.choices[0]
            content = getattr(choice.message, "content", None) or ""
            finish_reason = getattr(choice, "finish_reason", None)

            if finish_reason == "length":
                if max_tokens < self.max_tokens_upgrade:
                    max_tokens = self.max_tokens_upgrade  # 静默升档后重试
                    continue
                raise ToolError("批改结果过长且已达输出上限，请把文本缩短后重试")

            try:
                return parse_grade_content(content)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                if parse_retries < self.max_parse_retries:
                    parse_retries += 1
                    continue
                raise ToolError(f"批改模型返回的不是合法 JSON，重试后仍失败：{exc}") from exc
