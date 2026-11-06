"""学习尝试记录的领域模型、结果三档与 schema 校验（T07）。

一条尝试 = 用户做完一次短练习后、**经他明确确认**才落盘的事实。它只回答
"这次练了什么、他怎么答的、算不算独立做对"，不评分、不推断掌握度
（掌握度是另一套错题账本，见 ``memory/mastery.py``，T07 不碰）。

``attempt_id`` 由**作答内容**确定性派生（不含时间 / 证据 / 确认原话），
同一份作答因网络重试、重复点击而再次提交时算出同一个 id，从源头保证幂等。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..memory.sanitize import clip_bytes, sanitize_text
from ..memory.slug import normalize_fragment
from ..session.paths import local_now
from .errors import AttemptValidationError

__all__ = [
    "ATTEMPTS_VERSION",
    "AttemptRecord",
    "Outcome",
    "build_attempt",
    "derive_attempt_id",
]

ATTEMPTS_VERSION = 1

# 长字段单条字节预算：题目 / 作答 / 依据 / 确认原话都按此封顶，防单条无限大。
_FIELD_MAX_BYTES = 4 * 1024
# skill 是短标签（"一般过去时" / "VERB:TENSE"），单行、更紧。
_SKILL_MAX_BYTES = 200
_SESSION_MAX_BYTES = 512


class Outcome(StrEnum):
    """一次短练习的三种结果（任务书 T07 逐字契约，不得增删改名）。"""

    INDEPENDENT_CORRECT = "independent_correct"  # 没要提示，一次答对
    CORRECT_AFTER_HINT = "correct_after_hint"  # 给过提示后才答对
    NEEDS_PRACTICE = "needs_practice"  # 提示后仍错 / 放弃 / 还要练


@dataclass(frozen=True)
class AttemptRecord:
    """一条可核对的学习尝试。

    不可变：要更正只能软删旧记录、再让用户确认一条新的（任务书行为⑤——
    绝不静默改写用户的原始答案）。
    """

    attempt_id: str
    session_id: str
    skill: str
    prompt: str
    answer: str
    hint_used: bool
    outcome: Outcome
    evidence: str
    confirmation: str
    created_at: datetime
    confirmed_at: datetime | None = None


def derive_attempt_id(
    *,
    session_id: str,
    skill: str,
    prompt: str,
    answer: str,
    hint_used: bool,
    outcome: str,
) -> str:
    """由作答内容派生 12 位十六进制内容 id。

    身份字段 = 会话 + 知识点 + 题目 + 作答 + 是否用提示 + 结果。刻意**不含**
    created_at / evidence / confirmation：重试与重复提交时这几样可能变化，
    放进去会让同一次作答每次都算出新 id，幂等就形同虚设。
    """
    payload = "\x1f".join(
        [
            normalize_fragment(session_id),
            normalize_fragment(skill),
            normalize_fragment(prompt),
            normalize_fragment(answer),
            "hint" if hint_used else "nohint",
            normalize_fragment(outcome),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _require_text(
    raw: Any,
    field: str,
    *,
    max_bytes: int = _FIELD_MAX_BYTES,
    single_line: bool = False,
) -> str:
    """清洗 + 非空校验 + 字节封顶。空 / 纯空白一律拒绝。"""
    if not isinstance(raw, str):
        raise AttemptValidationError(f"{field} 必须是字符串")
    text = sanitize_text(raw)
    if single_line:
        # skill / session_id 是单行标量：把换行和连续空白折叠成单空格
        text = " ".join(text.split())
    if not text.strip():
        raise AttemptValidationError(f"{field} 不能为空")
    clipped, _ = clip_bytes(text, max_bytes)
    return clipped


def build_attempt(
    *,
    session_id: Any,
    skill: Any,
    prompt: Any,
    answer: Any,
    outcome: Any,
    evidence: Any,
    confirmation: Any,
    hint_used: Any = False,
    now: datetime | None = None,
) -> AttemptRecord:
    """校验入参并构建一条尝试记录（此时还没落盘，confirmed_at 为空）。

    任何不合法都抛 ``AttemptValidationError``，由工具层转成 ToolError 回灌模型，
    坏数据在进文件系统前就被挡住。
    """
    sid = _require_text(session_id, "session_id", max_bytes=_SESSION_MAX_BYTES, single_line=True)
    sk = _require_text(skill, "skill", max_bytes=_SKILL_MAX_BYTES, single_line=True)
    pr = _require_text(prompt, "prompt")
    an = _require_text(answer, "answer")
    ev = _require_text(evidence, "evidence")
    # 确认闸门：这是"用户明确同意保存"的唯一凭据，空 / 纯空白即视为未确认
    cf = _require_text(confirmation, "confirmation")

    # 必须是真 bool：bool("false") 在 Python 里为 True，靠 truthiness 会吞掉字符串
    if not isinstance(hint_used, bool):
        raise AttemptValidationError("hint_used 必须是布尔值 true / false")

    if isinstance(outcome, Outcome):
        oc = outcome
    else:
        try:
            oc = Outcome(outcome) if isinstance(outcome, str) else None
        except ValueError:
            oc = None
        if oc is None:
            allowed = ", ".join(o.value for o in Outcome)
            raise AttemptValidationError(f"outcome 必须是三档之一：{allowed}")

    ts = now or local_now()
    if not isinstance(ts, datetime):
        raise AttemptValidationError("created_at 必须是时间戳")

    attempt_id = derive_attempt_id(
        session_id=sid,
        skill=sk,
        prompt=pr,
        answer=an,
        hint_used=hint_used,
        outcome=oc.value,
    )
    return AttemptRecord(
        attempt_id=attempt_id,
        session_id=sid,
        skill=sk,
        prompt=pr,
        answer=an,
        hint_used=hint_used,
        outcome=oc,
        evidence=ev,
        confirmation=cf,
        created_at=ts,
    )
