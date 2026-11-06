"""ToolRegistry 测试。

切片 1：注册 → schema → 执行成功。
切片 2-4：错误即数据 —— 未知工具 / 参数非法 / 工具抛异常，都返回 ok=False，不抛异常。
"""

from collections.abc import Sequence
from typing import ClassVar

import pytest

from glyph.runtime.errors import ToolError
from glyph.runtime.registry import ToolRegistry
from glyph.runtime.tool import Tool


class EchoTool(Tool):
    """测试用工具：原样返回 text。"""

    name = "echo"
    description = "原样返回输入文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def run(self, args: dict) -> dict:
        return {"echoed": args["text"]}


class AddTool(Tool):
    """测试用工具：两数相加，要求 a/b 都是数字。"""

    name = "add"
    description = "两个数字相加"
    parameters = {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    }

    async def run(self, args: dict) -> dict:
        return {"sum": args["a"] + args["b"]}


class BoomTool(Tool):
    """工具作者主动抛 ToolError（业务做不成）。"""

    name = "boom"
    description = "总是抛业务错误"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict) -> dict:
        raise ToolError("业务上做不了：缺少已确认的原文")


class CrashTool(Tool):
    """工具内部出现未预期异常。"""

    name = "crash"
    description = "总是抛意外异常"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict) -> dict:
        raise RuntimeError("unexpected failure")


# ---------- 切片 1：成功路径 ----------


def test_register_then_list_schema() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    assert len(registry) == 1

    schemas: Sequence[dict] = registry.schemas()
    assert len(schemas) == 1
    schema = schemas[0]
    # 形状对齐 OpenAI / 千问 function calling
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["description"] == "原样返回输入文本"
    assert schema["function"]["parameters"]["required"] == ["text"]


async def test_execute_success_returns_ok_result_with_duration() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = await registry.execute("echo", {"text": "hello"})

    assert result.ok is True
    assert result.output == {"echoed": "hello"}
    assert result.error is None
    assert result.tool_name == "echo"
    assert result.duration_ms >= 0


def test_register_duplicate_name_fails_fast() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    with pytest.raises(ValueError, match="echo"):
        registry.register(EchoTool())


# ---------- 切片 2：未知工具（模型幻觉工具名）----------


async def test_execute_unknown_tool_returns_error_listing_available_tools() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())

    result = await registry.execute("does_not_exist", {})

    assert result.ok is False
    assert result.output is None
    assert result.tool_name == "does_not_exist"
    # 错误是人话，且告诉模型现在到底有哪些工具可用
    assert "does_not_exist" in result.error
    assert "echo" in result.error


# ---------- 切片 3：参数不合 JSON Schema ----------


async def test_execute_invalid_arguments_return_error_not_raise() -> None:
    registry = ToolRegistry()
    registry.register(AddTool())

    # 缺少必填的 b
    result = await registry.execute("add", {"a": 1})

    assert result.ok is False
    assert result.output is None
    assert result.tool_name == "add"
    assert result.error  # 非空，包含校验原因


async def test_execute_wrong_type_arguments_return_error() -> None:
    registry = ToolRegistry()
    registry.register(AddTool())

    result = await registry.execute("add", {"a": "not-a-number", "b": 2})

    assert result.ok is False
    assert "add" == result.tool_name


# ---------- 切片 4：工具抛异常 ----------


async def test_tool_error_becomes_error_result() -> None:
    registry = ToolRegistry()
    registry.register(BoomTool())

    result = await registry.execute("boom", {})

    assert result.ok is False
    assert result.output is None
    assert result.tool_name == "boom"
    assert "缺少已确认的原文" in result.error
    assert result.duration_ms >= 0


async def test_unexpected_exception_is_wrapped_not_raised() -> None:
    registry = ToolRegistry()
    registry.register(CrashTool())

    result = await registry.execute("crash", {})

    # 意外异常也不能炸穿循环，要包成错误结果并保留原因
    assert result.ok is False
    assert result.tool_name == "crash"
    assert "unexpected failure" in result.error


# ---------- 切片 5：只读 / 写入身份（R10）----------


class PlainWriteTool(Tool):
    """直接继承 Tool、不声明身份：默认按写入处理。"""

    name = "plain"
    description = "未声明身份的工具"
    parameters = {"type": "object", "properties": {}}

    async def run(self, args: dict) -> dict:
        return {}


def test_is_readonly_true_for_readonly_tool() -> None:
    from glyph.tools.base import ReadOnlyTool

    class Ro(ReadOnlyTool):
        name = "ro"
        description = "只读"
        parameters = {"type": "object", "properties": {}}

        async def run(self, args: dict) -> dict:
            return {}

    registry = ToolRegistry()
    registry.register(Ro())

    assert registry.is_readonly("ro") is True


def test_is_readonly_false_for_undeclared_and_write_tool() -> None:
    from glyph.tools.base import WriteTool

    class W(WriteTool):
        name = "w"
        description = "写入"
        parameters = {"type": "object", "properties": {}}

        async def run(self, args: dict) -> dict:
            return {}

    registry = ToolRegistry()
    registry.register(PlainWriteTool())
    registry.register(W())

    # 未声明身份默认写入；显式 WriteTool 也是写入
    assert registry.is_readonly("plain") is False
    assert registry.is_readonly("w") is False


def test_is_readonly_unknown_tool_conservatively_false() -> None:
    registry = ToolRegistry()

    # 未知工具名保守按写入处理，绝不能当成只读让它混进并发批次
    assert registry.is_readonly("ghost") is False


# ---------- 切片 6：交互式身份（等用户的工具不受工具超时管）----------


class AskTool(Tool):
    """声明 ``interactive``：要停下来等用户回答，可能等很久。"""

    name = "ask"
    description = "问用户一句"
    parameters = {"type": "object", "properties": {}}
    interactive: ClassVar[bool] = True

    async def run(self, args: dict) -> dict:
        return {}


def test_is_interactive_true_only_for_declared_tool() -> None:
    registry = ToolRegistry()
    registry.register(AskTool())
    registry.register(EchoTool())

    assert registry.is_interactive("ask") is True
    # 普通工具不声明交互式：默认 False，不能因为漏声明就让它免掉超时
    assert registry.is_interactive("echo") is False


def test_is_interactive_unknown_tool_false() -> None:
    registry = ToolRegistry()

    assert registry.is_interactive("ghost") is False
