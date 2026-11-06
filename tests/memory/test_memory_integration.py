"""长期记忆端到端集成测试（真模型，默认跳过，用 -m integration 运行）。

离线测试证明记忆工具与目录渲染各自正确；本测试证明整条链路在真模型下成立：

1. 用户说“记住：……”，真实主模型自主调用 remember，条目真实落盘；
2. 开一个**全新会话**（旧账本一条不带），只靠每轮注入的记忆目录，
   模型仍能知道这条偏好（直接答或先 read_memory 再答）。

运行（在 markr/ 目录）：
    .venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration \\
        tests/memory/test_memory_integration.py -s
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from glyph.agent_prompt import build_system_prompt
from glyph.config import load_settings
from glyph.context import env_date_fragment
from glyph.llm.qwen import QwenLLM
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.tools.factory import build_memory

# 复用 runtime 层的内存历史（各测试目录无 __init__，手动加入路径）
_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(_RUNTIME))
from fakes import InMemoryHistory  # noqa: E402


def _final_text(events: list) -> str:
    return "".join(getattr(e, "delta", "") for e in events if e.type == "text_delta")


@pytest.mark.integration
async def test_remember_then_recalled_in_fresh_session():
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过长期记忆真模型集成测试")

    store, mem_tools, injector = build_memory(settings, root=_tmp_root())
    registry = ToolRegistry()
    for tool in mem_tools:
        registry.register(tool)
    system = build_system_prompt(tools=registry.tools())
    llm = QwenLLM(settings)

    # --- 第一段会话：明确要求记住 ---
    history_one = InMemoryHistory(system_prompt=system)
    loop_one = AgentLoop(llm, registry, history=history_one)
    events_one = [e async for e in loop_one.run("记住：我希望你每次讲解都用简体中文。")]

    remember_calls = [
        e
        for e in events_one
        if e.type == "tool_call" and getattr(getattr(e, "call", None), "name", None) == "remember"
    ]
    assert remember_calls, "真实主模型没有自主调用 remember"
    results = [e for e in events_one if e.type == "tool_result"]
    assert results and results[0].ok is True, "remember 执行失败"

    # 条目真实落盘
    on_disk = store.list_entries()
    assert len(on_disk) == 1
    assert "简体中文" in on_disk[0].title or "简体中文" in on_disk[0].quote

    # --- 全新会话：旧账本全无，只靠注入的记忆目录 ---
    history_two = InMemoryHistory(
        system_prompt=system,
        context=[env_date_fragment(), injector.fragment],
    )
    loop_two = AgentLoop(llm, registry, history=history_two)
    events_two = [e async for e in loop_two.run("你知道我希望你用什么语言做讲解吗？")]

    # 允许模型先 read_memory 取细节（也可能直接凭目录回答）
    names = {
        getattr(getattr(e, "call", None), "name", None) for e in events_two if e.type == "tool_call"
    }
    assert names <= {"read_memory"}, f"新会话里不该出现别的工具调用，实际：{names}"

    answer = _final_text(events_two)
    assert "中文" in answer, f"模型没有回忆起语言偏好，回答：{answer!r}"


def _tmp_root() -> Path:
    """pytest 的 tmp_path 不进函数签名（集成测试手写一个临时目录）。"""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="markr-mem-it-"))
