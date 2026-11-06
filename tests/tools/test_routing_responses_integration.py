"""路由考卷 · **Responses 通道**真模型跑分（默认不跑）。

为什么必须有这份：现有 `test_routing_regression.py` 走的是 **Chat 通道**
（`client.chat.completions.create`），而产品实际装配的是 **Responses 通道**
（`web/server.py::_build_runtime` → `ResponsesLLM`）。两条通道的请求体、工具声明
形状、模型可见的工具清单都不一样 —— Responses 还多声明一个平台内置 `web_search`。
**只验 Chat 路径不能说明真实装配下的路由是对的**（T03 执行内容 1）。

跑法（会真的调用模型、真的花钱，默认不跑）：

    .venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration \\
        tests/tools/test_routing_responses_integration.py

省额度开关（与 Chat 版一致）：
    GLYPH_ROUTING_CASES=r1,r5   只跑指定题号
    GLYPH_ROUTING_MODEL=xxx     换便宜模型

与 Chat 版的关系：**同一份考卷、同一个门槛、两个通道各记各的分数**。
不互相覆盖基线 —— 分数是"某个日期、某个模型、某个通道"下的实测值，混在一起会丢信息。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from glyph.agent_prompt import build_system_prompt
from glyph.config import load_settings
from glyph.llm.base import ToolCallReady
from glyph.llm.responses import ResponsesLLM
from glyph.types import SystemMessage, UserMessage

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "routing" / "cases.json"

# 门槛与 Chat 版一致（0.70）：它是"跌破就报警"的下限，不是"必须考到 70 分"。
# ⚠️ 这是沿用 Chat 版的门槛；Responses 通道的首测实测值见本文件末尾的变更记录，
#    抬门槛必须附新日期、新通道、新分数。
MIN_ACCURACY = 0.70

pytestmark = pytest.mark.integration

_MAX_TOKENS = 512


class _Stub:
    """路由替身：借真工具的名字 / 描述 / 入参 schema，但不实现 run（与 Chat 版同思路）。

    为什么可以这么干：模型做路由决策时看到的就是这三样。真去执行工具会把
    "路由"和"工具质量"两件事混在一起，一条题就要几十秒。
    """

    def __init__(self, source: type) -> None:
        self.name = source.name
        self.description = source.description
        self.parameters = source.parameters


def _stub_sources() -> list:
    """与 Chat 版保持同一套工具清单：考卷声明的工具必须两边一致。"""
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

    return [
        GradeTextTool,
        OcrImageTool,
        DescribeImageTool,
        RecheckDetectionTool,
        RememberTool,
        ForgetTool,
        ReadMemoryTool,
        LookupWordTool,
        ReadFileTool,
        AnalyzeWritingTool,
    ]


def _schemas() -> list[dict]:
    out = []
    for src in _stub_sources():
        out.append(
            {
                "type": "function",
                "function": {
                    "name": src.name,
                    "description": src.description,
                    "parameters": src.parameters,
                },
            }
        )
    return out


async def _ask(llm: ResponsesLLM, system: str, user: str) -> str | None:
    """问一次，返回模型想调的第一个**本地**工具名；不调本地工具则返回 None。

    平台 web_search 不算本地工具调用（它由云端执行、不进注册表），
    所以这里只看 ToolCallReady。
    """
    events = []
    async for event in llm.stream_chat(
        [SystemMessage(content=system), UserMessage(content=user)],
        _schemas(),
        max_tokens=_MAX_TOKENS,
    ):
        events.append(event)

    for event in events:
        if isinstance(event, ToolCallReady):
            return event.call.name
    return None


@pytest.mark.integration
async def test_responses_通道路由准确率不低于门槛() -> None:
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过 Responses 通道路由跑分")

    try:
        import openai
    except ImportError as exc:  # pragma: no cover - 依赖缺失分支
        pytest.skip(f"未安装 openai 依赖：{exc}")

    exam = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = exam["cases"]

    only = os.environ.get("GLYPH_ROUTING_CASES", "").strip()
    if only:
        wanted = {s.strip() for s in only.split(",") if s.strip()}
        unknown = wanted - {c["id"] for c in cases}
        assert not unknown, f"GLYPH_ROUTING_CASES 里有不存在的题号：{sorted(unknown)}"
        cases = [c for c in cases if c["id"] in wanted]

    model = os.environ.get("GLYPH_ROUTING_MODEL") or settings.model

    # 关键：按**真实装配**来 —— web/server.py 不传 web_search，用默认 True，
    # 所以模型手里除了本地 function 还有平台 web_search。这也是本通道与 Chat 版的实质差异。
    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=120.0)
    llm = ResponsesLLM(settings, client=client, web_search=True)

    system = build_system_prompt(tools=[_Stub(s) for s in _stub_sources()])

    rows: list[tuple[str, str | None, str | None, bool]] = []
    try:
        for case in cases:
            actual = await _ask(llm, system, case["input"])
            acceptable = [case["expect"], *(case.get("also_ok") or [])]
            rows.append((case["id"], case["expect"], actual, actual in acceptable))
    finally:
        await client.close()

    wrong = [r for r in rows if not r[3]]
    accuracy = (len(rows) - len(wrong)) / len(rows) if rows else 0.0

    hit = len(rows) - len(wrong)
    print(f"\nResponses 通道路由：{hit}/{len(rows)} = {accuracy:.1%}（模型 {model}）")
    for cid, expect, actual, _ in wrong:
        print(f"  ✗ {cid}: 期望 {expect!r} 实际 {actual!r}")

    assert accuracy >= MIN_ACCURACY, (
        f"Responses 通道路由准确率 {accuracy:.1%} 低于门槛 {MIN_ACCURACY:.0%}；"
        f"错题：{[r[0] for r in wrong]}"
    )
