"""路由考卷的**真模型跑分**（`marker=integration`，默认不跑，守 R6）。

它回答一个此前没人能回答的问题：**模型面对真实用户说法，到底会不会调错工具？错多少？**

做法（刻意做得很轻）：
- 只打**一轮** function calling，问模型"这句话你打算调哪个工具"，然后看它第一个
  tool call 的名字。不跑完整 loop —— 我们要测的是**路由决策**，不是工具执行得好不好。
- 工具是**替身**：只借用真工具的 `name` / `description` / `parameters`，
  `run` 一律抛 `NotImplementedError`。路由只看这三样，替身不会让分数失真；
  反过来，如果这里真去调视觉模型 / 批改模型，一条题就要几十秒和真金白银。
- `temperature=0`，尽量可复现；但**真模型仍有随机性**，分数请看成一个区间而不是定值。

⚠️ **一次跑满 = 24 次模型调用**，花的都是主模型（`settings.model`，默认 deepseek-v4-pro）
的额度。人 2026-09-25 明确说过 pro 额度快用完了，所以这里给了两个省着用的开关：

    # 只跑指定的几题（逗号分隔题号），调提示词时只验证受影响的题
    set GLYPH_ROUTING_CASES=recheck-disagree,recheck-rejudge

    # 换一个便宜的模型来跑（不改项目配置，只影响这一次跑分）
    set GLYPH_ROUTING_MODEL=deepseek-v4-flash

    .venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration \\
        tests/tools/test_routing_regression.py -s

`-s` 要看逐题对错表 —— 错题才是这份考卷最有价值的产出。
**只跑子集时不判分**（样本太小，没有统计意义），只打印对错表。

分数怎么用：
- `MIN_ACCURACY` 是**基线**，不是目标。它的作用是"不许变差"：改完提示词如果掉下去，
  这里就红。想把基线往上抬，必须先在提示词里真的改好，再改这个常量并写明日期与分数。
- ⚠️ 真模型**有抖动**：同一份考卷、temperature=0，连跑三次得到 84% / 79% / 84%
  （其中 `image-transcribe-only` 一题两次结果不同）。分数请当成**区间**看，
  不要因为一两条题的波动就改提示词。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from glyph.agent_prompt import build_system_prompt
from glyph.config import load_settings
from glyph.tools import (
    DescribeImageTool,
    GradeTextTool,
    OcrImageTool,
    RecheckDetectionTool,
)
from glyph.tools.analyze_writing import AnalyzeWritingTool
from glyph.tools.file_tools import ReadFileTool
from glyph.tools.lookup_word import LookupWordTool
from glyph.tools.memory_tools import ForgetTool, ReadMemoryTool, RememberTool

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "routing" / "cases.json"

# 2026-09-25 首测基线（deepseek-v4-pro，跑满 19 题，连跑三次：84% / 79% / 84%）。
# ⚠️ 这是"当时跑出来的分数"，不是"我们要求的分数"。抬基线必须附新日期与新分数。
# ⚠️ 门槛刻意压在实测值下方一截（70% vs 实测 79~84%）：真模型有抖动，
#    门槛的作用是"跌破就报警"，不是"必须考到 70 分"。
MIN_ACCURACY = 0.70

pytestmark = pytest.mark.integration

# 只问"你要调哪个工具"，不需要长输出；给够工具调用的空间即可
_MAX_TOKENS = 512


class _RoutingStub:
    """路由替身：借真工具的名字 / 描述 / 入参 schema，但不实现 run。

    为什么可以这么干：模型做路由决策时看到的就是这三样（`ToolRegistry.schemas()`
    发给模型的也只有这三样）。真去执行工具会把"路由"和"工具质量"两件事混在一起，
    一条题就要几十秒。
    """

    def __init__(self, source: type) -> None:
        self.name = source.name
        self.description = source.description
        self.parameters = source.parameters

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("路由考卷只测决策，不执行工具")


def _stub_tools() -> list[_RoutingStub]:
    return [
        _RoutingStub(GradeTextTool),
        _RoutingStub(OcrImageTool),
        _RoutingStub(DescribeImageTool),
        _RoutingStub(RecheckDetectionTool),
        _RoutingStub(RememberTool),
        _RoutingStub(ForgetTool),
        _RoutingStub(ReadMemoryTool),
        _RoutingStub(LookupWordTool),
        _RoutingStub(ReadFileTool),
        _RoutingStub(AnalyzeWritingTool),
    ]


def _schemas(tools: list[_RoutingStub]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


async def _ask_model(client: Any, model: str, system: str, user: str) -> str | None:
    """问一次，返回模型想调的第一个工具名；不调工具则返回 None。"""
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tools=_schemas(_stub_tools()),
        temperature=0,
        max_tokens=_MAX_TOKENS,
    )
    message = response.choices[0].message
    calls = getattr(message, "tool_calls", None) or []
    if not calls:
        return None
    return calls[0].function.name


@pytest.mark.integration
async def test_路由准确率不低于基线() -> None:
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过路由考卷真模型跑分")

    try:
        import openai
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        pytest.skip(f"未安装 openai 依赖：{exc}")

    exam = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = exam["cases"]

    # 省额度开关：只跑指定题号（调提示词时只想验证受影响的题）
    only = os.environ.get("GLYPH_ROUTING_CASES", "").strip()
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        unknown = wanted - {c["id"] for c in cases}
        assert not unknown, f"GLYPH_ROUTING_CASES 里有不存在的题号：{sorted(unknown)}"
        cases = [c for c in cases if c["id"] in wanted]

    # 省额度开关：换便宜模型跑（默认仍用 settings.model）
    model = os.environ.get("GLYPH_ROUTING_MODEL") or settings.model

    tools = _stub_tools()
    system = build_system_prompt(tools=tools)

    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=120.0)

    rows: list[tuple[str, str | None, str | None, bool]] = []
    try:
        for case in cases:
            actual = await _ask_model(client, model, system, case["input"])
            acceptable = [case["expect"], *(case.get("also_ok") or [])]
            ok = actual in acceptable
            rows.append((case["id"], case["expect"], actual, ok))
    finally:
        await client.close()

    hits = sum(1 for r in rows if r[3])
    accuracy = hits / len(rows)

    print(f"\n[routing] model={model}  题量={len(rows)}  命中={hits}")
    print(f"[routing] 准确率 = {accuracy:.0%}（基线 {MIN_ACCURACY:.0%}）\n")
    print(f"{'题号':<28}{'期望':<18}{'实际':<18}结果")
    for case_id, expect, actual, ok in rows:
        print(f"{case_id:<28}{str(expect):<18}{str(actual):<18}{'✅' if ok else '❌'}")

    misses = [r for r in rows if not r[3]]
    if misses:
        print("\n[routing] 错题（最有价值的部分）：")
        for case_id, expect, actual, _ in misses:
            why = next(c["why"] for c in cases if c["id"] == case_id)
            print(f"  - {case_id}: 期望 {expect} / 实际 {actual}")
            print(f"      依据：{why}")

    if len(rows) < len(exam["cases"]):
        print(
            f"\n[routing] 只跑了 {len(rows)}/{len(exam['cases'])} 题（GLYPH_ROUTING_CASES），"
            "样本太小不判分 —— 要看总分请跑满全部题目。"
        )
        return

    assert accuracy >= MIN_ACCURACY, (
        f"路由准确率 {accuracy:.0%} 低于基线 {MIN_ACCURACY:.0%}；"
        f"错题：{[r[0] for r in misses]}（上面有逐题对错表）"
    )


def test_桩工具表与考卷声明的工具一致() -> None:
    """发给模型的工具 schema 必须覆盖考卷声明的全部工具。

    2026-09-27 踩过：考卷补了 lookup_word / read_file / analyze_writing 三条题，
    但 `_stub_tools()` 里还是老七样 —— 模型压根看不到这三个工具，
    六道正向题**必然**全错，准确率被拉到 69%（跌破 70% 基线），
    而错因被误读成"模型不肯调工具"。

    这条守线保证：考卷加工具而桩没跟上 → 立刻红，不再靠跑完 32 题才发现。
    """
    exam = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    declared = set(exam.get("tools", []))
    stubbed = {t.name for t in _stub_tools()}
    assert declared == stubbed, (
        f"考卷工具与测试桩工具不一致：考卷多 {declared - stubbed} / 桩多 {stubbed - declared}"
    )
