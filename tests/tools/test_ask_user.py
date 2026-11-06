"""ask_user 工具离线契约测试（R6：不联网、不调真模型）。

覆盖的是**模型侧契约**：
1. 参数不合规（选项数 / 重复 / 空问题）时，工具**不报错**，而是回一段"纠偏文本"
   教模型改（抄 Claude Code 的 validationErrorSteer：用户无感、模型自我纠正、
   明令禁止编造凑数选项）。因此这些用例一律断言 ``status == "steer"``；
2. 正常回答时，回灌给模型的 ``summary`` 是一句**人话**且分三档语气
   （选了选项 / 自己打字 / 没等来回答），不是裸 JSON；
3. 同一时刻只允许一个待回答的问题（界面上弹两张卡片会让用户懵）。
"""

from __future__ import annotations

import asyncio

import pytest

from glyph.tools.ask_user import AskUserGateway, AskUserTool

TWO_OPTIONS = [
    {"label": "先批改语法", "description": "只挑语法和用词错误，给逐条讲解。"},
    {"label": "先练口语", "description": "围绕这个话题做五分钟对话练习。"},
]


def _tool_and_gateway() -> tuple[AskUserTool, AskUserGateway]:
    gateway = AskUserGateway()
    return AskUserTool(gateway), gateway


async def _ask_with(tool: AskUserTool, gateway: AskUserGateway, **overrides):
    """发起一次询问并在后台作答，返回工具结果。"""
    args = {"question": "你想先做哪一样？", "options": TWO_OPTIONS}
    args.update(overrides)
    task = asyncio.ensure_future(tool.run(args))
    # 等工具真正挂起（pending 出现）再作答，避免竞态
    for _ in range(50):
        if gateway.has_pending:
            break
        await asyncio.sleep(0)
    if gateway.has_pending:
        gateway.answer(choice="先批改语法")
    return await task


# ---------------------------------------------------------------- 参数纠偏


async def test_single_option_is_steered_not_rejected():
    """只给一个选项：不算失败，回纠偏文本，且**不许去问用户**。"""
    tool, gateway = _tool_and_gateway()
    single = [{"label": "只有这个", "description": "没别的选择"}]
    out = await tool.run({"question": "要走哪条路？", "options": single})
    assert out["status"] == "steer"
    assert "凑数" in out["summary"] or "不要再问" in out["summary"]
    assert gateway.has_pending is False


async def test_more_than_four_options_is_steered():
    tool, gateway = _tool_and_gateway()
    many = [{"label": f"选项{i}", "description": "说明"} for i in range(5)]
    out = await tool.run({"question": "选哪个？", "options": many})
    assert out["status"] == "steer"
    assert "4" in out["summary"]
    assert gateway.has_pending is False


async def test_duplicate_labels_are_steered():
    tool, gateway = _tool_and_gateway()
    dup = [
        {"label": "同一个", "description": "说明甲"},
        {"label": "同一个", "description": "说明乙"},
    ]
    out = await tool.run({"question": "选哪个？", "options": dup})
    assert out["status"] == "steer"
    assert "重复" in out["summary"]
    assert gateway.has_pending is False


async def test_empty_question_is_steered():
    tool, gateway = _tool_and_gateway()
    out = await tool.run({"question": "   ", "options": TWO_OPTIONS})
    assert out["status"] == "steer"
    assert gateway.has_pending is False


# ---------------------------------------------------------------- 回答三档


async def test_choice_answer_reads_as_sentence():
    """选了选项：summary 是一句人话，带得回用户选的那一项。"""
    tool, _ = await _make_answered(choice="先批改语法")
    out = tool
    assert out["status"] == "answered"
    assert out["choice"] == "先批改语法"
    assert "先批改语法" in out["summary"]


async def test_custom_text_answer_carries_warning():
    """用户自己打字：必须提醒模型"他可能是要你改 / 不让你这么做"。"""
    tool, gateway = _tool_and_gateway()
    out = await _run_with_answer(tool, gateway, text="先别改，帮我翻译这句话")
    assert out["status"] == "answered"
    assert out["custom_text"] == "先别改，帮我翻译这句话"
    assert "照" in out["summary"] and "用户" in out["summary"]


async def test_unanswered_reads_as_no_answer():
    """没等来回答（关窗口 / 用户没理）：如实说没答，让模型自己判断继续。"""
    tool, gateway = _tool_and_gateway()
    task = asyncio.ensure_future(
        tool.run({"question": "你想先做哪一样？", "options": TWO_OPTIONS})
    )
    for _ in range(50):
        if gateway.has_pending:
            break
        await asyncio.sleep(0)
    gateway.abandon(reason="窗口关闭")
    out = await task
    assert out["status"] == "no_answer"
    assert "没有等到" in out["summary"]


# ---------------------------------------------------------------- 一次一个


async def test_only_one_pending_question_at_a_time():
    """第二个问题在第一个没答完时，直接吃纠偏文本，不排队、不弹第二张卡片。"""
    tool, gateway = _tool_and_gateway()
    first = asyncio.ensure_future(
        tool.run({"question": "先做哪样？", "options": TWO_OPTIONS})
    )
    for _ in range(50):
        if gateway.has_pending:
            break
        await asyncio.sleep(0)
    second = await tool.run({"question": "还有第二个？", "options": TWO_OPTIONS})
    assert second["status"] == "steer"
    assert "一次" in second["summary"]

    gateway.answer(choice="先批改语法")
    assert (await first)["status"] == "answered"


# ---------------------------------------------------------------- 辅助


async def _make_answered(choice: str):
    tool, gateway = _tool_and_gateway()
    out = await _run_with_answer(tool, gateway, choice=choice)
    return out, gateway


async def _run_with_answer(tool: AskUserTool, gateway: AskUserGateway, **answer):
    task = asyncio.ensure_future(
        tool.run({"question": "你想先做哪一样？", "options": TWO_OPTIONS})
    )
    for _ in range(50):
        if gateway.has_pending:
            break
        await asyncio.sleep(0)
    gateway.answer(**answer)
    return await task


def test_gateway_answer_without_pending_returns_false():
    gateway = AskUserGateway()
    assert gateway.answer(choice="没人问也能答吗") is False


def test_tool_declares_schema():
    tool, _ = _tool_and_gateway()
    assert tool.name == "ask_user"
    assert tool.parameters["properties"]["options"]["type"] == "array"
    # 数量上限交给应用层纠偏（要回人话），schema 只保证类型正确
    assert "maxItems" not in tool.parameters["properties"]["options"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------- 对账


async def test_answer_is_rejected_when_question_does_not_match():
    """答复带回来的问题对不上当前挂着的那句：拒收，答案不能进错问题。

    场景：上一轮的卡片还留在界面上（或另一个窗口里），用户点了一下 ——
    那次点击属于一个已经结束的提问。收下它会让模型拿着一个完全不相干的决定往下走，
    而界面一片安静，谁也不会发现。
    """
    tool, gateway = _tool_and_gateway()
    task = asyncio.ensure_future(
        tool.run({"question": "这篇要先改还是先讲思路？", "options": TWO_OPTIONS})
    )
    for _ in range(50):
        if gateway.has_pending:
            break
        await asyncio.sleep(0)

    assert gateway.answer(choice="先批改", question="一个早就不存在的问题？") is False
    assert gateway.has_pending is True  # 还在等，没被那次点击顶掉

    # 对上了才收
    assert gateway.answer(choice="先批改", question="这篇要先改还是先讲思路？") is True
    out = await task
    assert out["choice"] == "先批改"


async def test_answer_without_question_skips_the_check():
    """不传问题就只对"有没有挂着"负责 —— 一次只问一个时这已经够了，别给调用方加负担。"""
    tool, gateway = _tool_and_gateway()
    task = asyncio.ensure_future(
        tool.run({"question": "这篇要先改还是先讲思路？", "options": TWO_OPTIONS})
    )
    for _ in range(50):
        if gateway.has_pending:
            break
        await asyncio.sleep(0)

    assert gateway.answer(choice="先讲思路") is True
    assert (await task)["choice"] == "先讲思路"
