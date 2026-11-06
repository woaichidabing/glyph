"""路由考卷的**离线自检**（不联网，R6）。

考卷本身是数据（`tests/fixtures/routing/cases.json`），真模型打分是
`test_routing_regression.py`（`marker=integration`）。但**数据错了，分数再高也是假的**，
所以这份自检必须默认就跑，盯四件事：

1. 每条题的"标准答案"必须落在**真实存在的工具名**里（或 `null` = 不该调工具）。
   工具改名 / 删掉之后，考卷里的旧名字会变成永远对不上的死题。
2. 考卷里的工具清单必须和 `agent_prompt._default_tools()` 对得上 ——
   新增一个工具却没进考卷，等于这个工具从没被测过路由。
3. **必须同时有"该调"和"不该调"两类题**。只有正例的考卷会奖励一个"什么都想调"
   的模型，那正是我们最怕的行为。
4. 每条题都要写 `why`（凭什么说答案是这个）—— 没写理由的题，
   将来模型答错了，我们没法判断是模型错还是答案错。

这里**不**断言准确率：准确率属于真模型测试的活（R6，默认不跑）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from glyph.agent_prompt import _default_tools

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "routing" / "cases.json"


def _load() -> dict:
    """模块级加载：让下面的用例能按**真实题号**参数化，而不是猜一个上限再 skip。"""
    if not CASES_PATH.is_file():
        return {}
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


_EXAM = _load()
_CASE_IDS = [case["id"] for case in _EXAM.get("cases", []) if isinstance(case, dict)]


def test_考卷文件存在且结构合法() -> None:
    assert CASES_PATH.is_file(), f"路由考卷不存在：{CASES_PATH}"
    assert _EXAM, "考卷顶层必须是非空对象"
    assert _EXAM.get("version") == 1, "考卷版本号不对，读这份测试的人要确认格式没变"
    assert isinstance(_EXAM.get("tools"), list) and _EXAM["tools"], "考卷必须声明工具清单"
    assert _CASE_IDS, "考卷里一条题都没有"


def test_考卷声明的工具清单与系统词默认清单一致() -> None:
    """考卷里的工具名必须是**真实**工具，且和默认清单一一对应。

    只比对名字集合：新增工具而考卷没跟上 → 这里红，逼人补题。
    """
    declared = set(_EXAM.get("tools", []))
    actual = {tool.name for tool in _default_tools()}
    assert declared == actual, (
        f"考卷工具清单与 agent_prompt._default_tools() 不一致："
        f"考卷多 {declared - actual} / 考卷少 {actual - declared}"
    )


def test_题号不重复() -> None:
    dup = {i for i in _CASE_IDS if _CASE_IDS.count(i) > 1}
    assert not dup, f"有重复的题号：{sorted(dup)}"


@pytest.mark.parametrize("case_id", _CASE_IDS)
def test_每条题的结构合法(case_id: str) -> None:
    case = next(c for c in _EXAM["cases"] if c["id"] == case_id)
    allowed = set(_EXAM["tools"]) | {None}

    assert case["input"].strip(), "用户输入是空的"
    assert case["expect"] in allowed, f"标准答案 {case['expect']!r} 既不是合法工具名，也不是 null"
    for name in case.get("also_ok", []):
        assert name in _EXAM["tools"], f"also_ok 里有不存在的工具 {name!r}"
    assert case["why"].strip(), "没写 why（凭什么答案是这个）"


def test_正负例都要有() -> None:
    """只有"该调"的题会奖励一个什么都想调的模型 —— 必须两类都有。"""
    expects = [case["expect"] for case in _EXAM["cases"]]
    positive = [e for e in expects if e is not None]
    negative = [e for e in expects if e is None]

    assert len(negative) >= 3, f"「不该调工具」的题只有 {len(negative)} 条，至少要 3 条"
    assert len(positive) >= 5, f"「该调工具」的题只有 {len(positive)} 条，至少要 5 条"


def test_四个工具每个都被考到() -> None:
    """新增工具却不考它，等于它的路由从没被验证过。"""
    covered = {case["expect"] for case in _EXAM["cases"] if case["expect"] is not None}
    for name in _EXAM["tools"]:
        assert name in covered, f"工具 {name} 在考卷里一条正例都没有"


def test_题量够用() -> None:
    """太少说明不了问题：至少 15 条才谈得上"一个分数"。"""
    assert len(_EXAM["cases"]) >= 15, f"考卷只有 {len(_EXAM['cases'])} 条题，至少要 15 条"
