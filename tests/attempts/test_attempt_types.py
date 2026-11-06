"""学习尝试记录的领域模型与 schema 校验（T07，离线）。

守的是"一条可核对的尝试"在进入存储之前就必须合法：

- ``outcome`` 只能是三档之一（独立答对 / 提示后答对 / 仍需练习）；
- skill / prompt / answer / evidence / confirmation 缺一不可；
- ``confirmation`` 是**用户确认闸门**：空、纯空白都不许过——模型自己的总结
  不能冒充用户确认；
- ``attempt_id`` 由内容确定性派生：同一份作答重试 / 重复提交是同一条（幂等），
  题目或答案变了才是新记录；时间、判定依据、确认原话**不进身份**
  （网络重试时它们可能变化，否则每次重试都算出新 id，幂等就破了）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from glyph.attempts import AttemptValidationError, Outcome
from glyph.attempts.types import build_attempt

# 固定时钟：身份派生不含时间，测试用同一时刻 / 不同时刻对照
_T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 10, 3, 18, 30, 0, tzinfo=UTC)


def _valid(**over: object) -> dict:
    """一份合法参数；单个用例只覆盖其中一个字段。"""
    base: dict = {
        "session_id": "session-20261003-120000-abcdef",
        "skill": "一般过去时",
        "prompt": "Yesterday I ___ (go) home.",
        "answer": "goed",
        "hint_used": False,
        "outcome": "needs_practice",
        "evidence": "一般过去时 go 的过去式是 went，用户写了 goed。",
        "confirmation": "好的，记下来",
    }
    base.update(over)
    return base


def test_合法记录构建且字段齐全() -> None:
    rec = build_attempt(now=_T0, **_valid())
    assert rec.session_id == "session-20261003-120000-abcdef"
    assert rec.skill == "一般过去时"
    assert rec.prompt == "Yesterday I ___ (go) home."
    assert rec.answer == "goed"
    assert rec.hint_used is False
    assert rec.outcome is Outcome.NEEDS_PRACTICE
    assert rec.evidence.startswith("一般过去时")
    assert rec.confirmation == "好的，记下来"
    assert rec.created_at == _T0
    # confirmed_at 在落盘那一刻才由 store 填，构建阶段为空
    assert rec.confirmed_at is None
    assert rec.attempt_id  # 非空


def test_三档结果枚举值逐字() -> None:
    # 这三个字符串会写进文件、被测试和提示词引用，漂移即红
    assert Outcome.INDEPENDENT_CORRECT.value == "independent_correct"
    assert Outcome.CORRECT_AFTER_HINT.value == "correct_after_hint"
    assert Outcome.NEEDS_PRACTICE.value == "needs_practice"


@pytest.mark.parametrize("bad", ["", "mastered", "correct", "wrong", "INDEPENDENT_CORRECT", "对了"])
def test_非法outcome被拒(bad: str) -> None:
    with pytest.raises(AttemptValidationError):
        build_attempt(now=_T0, **_valid(outcome=bad))


@pytest.mark.parametrize(
    "field",
    ["session_id", "skill", "prompt", "answer", "evidence", "confirmation"],
)
def test_必填字段空值被拒(field: str) -> None:
    with pytest.raises(AttemptValidationError):
        build_attempt(now=_T0, **_valid(**{field: ""}))


@pytest.mark.parametrize("blank", ["   ", "\n\t", "  \n  "])
def test_确认闸门纯空白被拒(blank: str) -> None:
    # confirmation 是"用户明确确认"的唯一凭据；纯空白等于没有确认
    with pytest.raises(AttemptValidationError):
        build_attempt(now=_T0, **_valid(confirmation=blank))


def test_hint_used默认False且接受True() -> None:
    assert build_attempt(now=_T0, **_valid()).hint_used is False
    hinted = _valid(hint_used=True, outcome="correct_after_hint")
    assert build_attempt(now=_T0, **hinted).hint_used is True


def test_非布尔hint_used被拒() -> None:
    # bool("false") 在 Python 里是 True，绝不能靠 truthiness 吞字符串
    with pytest.raises(AttemptValidationError):
        build_attempt(now=_T0, **_valid(hint_used="false"))  # type: ignore[arg-type]


def test_attempt_id是12位十六进制() -> None:
    rec = build_attempt(now=_T0, **_valid())
    assert len(rec.attempt_id) == 12
    assert all(c in "0123456789abcdef" for c in rec.attempt_id)


def test_同载荷不同时间算出同一id_重试幂等的根基() -> None:
    first = build_attempt(now=_T0, **_valid())
    retry = build_attempt(now=_T1, **_valid())  # 网络重试：时间晚了很多、其余完全一样
    assert first.attempt_id == retry.attempt_id


def test_证据或确认原话变化不改变id() -> None:
    # 重试时模型可能把 evidence 写得略详、confirmation 取到不同一轮的原话，
    # 这些都不是"这次作答"的身份，不该因此记成两条
    first = build_attempt(now=_T0, **_valid())
    again = build_attempt(
        now=_T1,
        **_valid(
            evidence="一般过去时 go→went，用户写成 goed，属规则过度泛化。",
            confirmation="确认",
        ),
    )
    assert first.attempt_id == again.attempt_id


@pytest.mark.parametrize(
    ("field", "new_value"),
    [
        ("answer", "went"),
        ("prompt", "Yesterday she ___ (go) home."),
        ("skill", "现在完成时"),
        ("session_id", "session-20261004-090000-zzzzzz"),
    ],
)
def test_身份字段变化产生新id(field: str, new_value: str) -> None:
    first = build_attempt(now=_T0, **_valid())
    second = build_attempt(now=_T0, **_valid(**{field: new_value}))
    assert first.attempt_id != second.attempt_id


def test_hint或outcome变化产生新id() -> None:
    first = build_attempt(now=_T0, **_valid())
    hinted = build_attempt(now=_T0, **_valid(hint_used=True, outcome="correct_after_hint"))
    assert first.attempt_id != hinted.attempt_id


def test_字段被清洗_换行归一与控制符去除() -> None:
    rec = build_attempt(
        now=_T0,
        **_valid(prompt="Line1\r\nLine2\x00", answer=" goed "),
    )
    assert "\r" not in rec.prompt
    assert "\x00" not in rec.prompt
    assert rec.prompt == "Line1\nLine2"
    # 作答原文保留内容，仅做安全清洗（不 strip 掉用户真实空格由存储侧决定，这里只去控制符）
    assert "goed" in rec.answer
