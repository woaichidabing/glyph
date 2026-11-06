"""主循环正常路径打点的测试（caplog 断言，不联网）。

设计稿 §0.2 的诊断：全仓原有 22 处日志**全在异常 / 降级路径**，正常路径长什么样
（模型每轮请求、每次工具执行、每次运行终态）**零打点**——所以"一切正常但行为怪"
（模型慢、循环转了几轮、哪个工具跑了几秒）依然全盲。

本文件守的就是补上的这几条正常路径打点，以及**脱敏**（正文绝不进日志）。

装配走 tests/runtime/fakes.py 的现成假件（R3 先查现成），不另造一套。
"""

from __future__ import annotations

import logging

from fakes import (
    FakeTurn,
    HangingStreamingLLM,
    InMemoryHistory,
    ScriptedFakeStreamingLLM,
    ScriptedTool,
)

from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.types import Usage


def _loop(llm, tools=None) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools or []:
        registry.register(tool)
    return AgentLoop(llm, registry, history=InMemoryHistory())


async def _drain(loop: AgentLoop, text: str = "你好") -> list:
    return [event async for event in loop.run(text)]


def _messages(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


async def test_turn_end_logs_one_line(caplog) -> None:
    """每轮结束一条 INFO：iter / stop / tokens / 用时——"循环转了几轮"不再靠猜。"""
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="好的")])
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(_loop(llm))

    lines = _messages(caplog, "轮结束")
    assert len(lines) == 1
    assert "iter=1" in lines[0]


async def test_run_end_logs_one_line(caplog) -> None:
    """运行终态一条 INFO：reason / iters——时间轴的收尾。"""
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="好的")])
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(_loop(llm))

    done = _messages(caplog, "运行结束")
    assert len(done) == 1
    assert "reason=final_text" in done[0]
    assert "iters=1" in done[0]


async def test_cancelled_run_still_logs_reason(caplog) -> None:
    """取消也走同一条终态日志：能对上用户说的「我点了停止」。

    用现成的 HangingStreamingLLM（流一开就挂住），在收到第一个 delta 时取消 ——
    与 test_loop.py 里那条取消测试同一套路，不另造假件（R3）。
    """
    llm = HangingStreamingLLM()
    loop = _loop(llm)
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        async for event in loop.run("问"):
            if event.type == "text_delta":
                loop.cancel()

    done = _messages(caplog, "运行结束")
    assert done, "取消也必须留下终态日志"
    assert "cancelled" in done[0]


async def test_tool_result_logs_one_line(caplog) -> None:
    """每个工具一条：名字 / 成功失败 / 耗时 / 参数摘要——"哪个工具跑了几秒"有据可查。"""
    tool = ScriptedTool("demo_tool", result={"ok": True})
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("demo_tool", {"text": "hello"})]),
            FakeTurn(text="好了"),
        ]
    )
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(_loop(llm, tools=[tool]))

    lines = _messages(caplog, "工具 demo_tool")
    assert len(lines) == 1
    assert "ok=True" in lines[0]


async def test_tool_log_never_contains_argument_text(caplog) -> None:
    """**脱敏红线**：参数里的长文本（可能是学生作文）绝不许出现在日志里。"""
    secret = "My favourite subject is English and I like it very much."
    tool = ScriptedTool("grade_text", result={})
    llm = ScriptedFakeStreamingLLM(
        [
            FakeTurn(tool_calls=[("grade_text", {"text": secret})]),
            FakeTurn(text="好了"),
        ]
    )
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(_loop(llm, tools=[tool]))

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "favourite" not in blob
    assert "English" not in blob
    # 但长度要有，才能定位"传了多大"
    assert "字" in blob


async def test_logs_carry_tokens_and_duration(caplog) -> None:
    """用量与耗时都要带上：模型变慢、省了多少，都从这两行读。"""
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="好的")])
    loop = _loop(llm)
    loop.llm._usage = Usage(input_tokens=100, output_tokens=20)
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(loop)

    turn = _messages(caplog, "轮结束")[0]
    assert "in=100" in turn
    assert "out=20" in turn
    assert "用时=" in turn


async def test_run_end_reports_total_tokens(caplog) -> None:
    """终态报的是**累加总数**（计费口径），不是最后一轮。"""
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="好的")])
    loop = _loop(llm)
    loop.llm._usage = Usage(input_tokens=100, output_tokens=20)
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(loop)

    done = _messages(caplog, "运行结束")[0]
    assert "in=100" in done
    assert "out=20" in done


async def test_no_info_logs_when_level_is_warning(caplog) -> None:
    """级别门槛生效：设成 WARNING 时这几条 INFO 一条都不该出（可级别过滤）。"""
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="好的")])
    with caplog.at_level(logging.WARNING, logger="glyph.runtime"):
        await _drain(_loop(llm))

    assert not _messages(caplog, "轮结束")
    assert not _messages(caplog, "运行结束")


async def test_logs_are_single_line(caplog) -> None:
    """每轮一行：结构化 key=value，能 grep；不许出现换行把日志撑成多行。"""
    llm = ScriptedFakeStreamingLLM([FakeTurn(text="好的")])
    with caplog.at_level(logging.INFO, logger="glyph.runtime"):
        await _drain(_loop(llm))

    for message in _messages(caplog, "轮结束") + _messages(caplog, "运行结束"):
        assert "\n" not in message
