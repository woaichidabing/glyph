"""工具注册表：注册 / 查找 / 列出 schema / 统一执行。

loop 只通过注册表接触工具，严禁按工具名写 if/else 分支（铁律 R2）。

核心约定（错误即数据）：``execute`` 对运行期的任何失败 —— 未知工具名（模型幻觉）、
参数不合 schema、工具抛 ``ToolError`` 或任意异常 —— 都返回一条 ``ok=False`` 的
``ToolResult``，而不是抛异常。只有"工具配置错误"（重名、schema 本身写错）才在
注册 / 执行前 fail-fast 抛出，因为那是开发者要修的 bug，不是模型能补救的情况。
"""

from __future__ import annotations

import time
from typing import Any

import jsonschema

from .errors import ToolError
from .tool import Tool, ToolResult

# 统一用 Draft 2020-12 校验工具入参
_VALIDATOR_CLS = jsonschema.Draft202012Validator


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具。空名 / 同名重复直接报错（配置期 fail-fast，防止静默覆盖）。"""
        name = getattr(tool, "name", "")
        if not name:
            raise ValueError("工具必须有非空 name")
        if name in self._tools:
            raise ValueError(f"工具 '{name}' 已注册，不能重复注册")
        # 提前校验工具自带的 JSON Schema 本身合法，错误在配置期暴露
        try:
            _VALIDATOR_CLS.check_schema(tool.parameters)
        except jsonschema.SchemaError as exc:
            raise ValueError(f"工具 '{name}' 的 JSON Schema 非法: {exc.message}") from exc
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def is_readonly(self, name: str) -> bool:
        """工具是否只读（R10）。未知工具名保守按写入处理（False）。

        是全项目唯一读取 ``Tool.readonly`` 身份的地方：``AgentLoop`` 据此
        决定整批并发（全只读）还是整批串行（含写入）。
        """
        tool = self._tools.get(name)
        return bool(getattr(tool, "readonly", False))

    def is_interactive(self, name: str) -> bool:
        """工具是否要停下来等用户（``Tool.interactive``）。未知工具名保守按 False。

        是全项目唯一读取该身份的地方：``AgentLoop`` 据此决定要不要给这个工具套
        ``tool_timeout``。交互式工具必须豁免——等一个人点选项可能几分钟，
        掐断它只会在用户还没看到问题时就把这一轮以"工具超时"收尾。
        """
        tool = self._tools.get(name)
        return bool(getattr(tool, "interactive", False))

    def tools(self) -> list[Tool]:
        """返回当前已注册的全部工具（只读列表），供装配层按实际清单重建提示词等。"""
        return list(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """返回 OpenAI / 千问 function calling 形状的工具清单，随每次请求发给模型。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        """按名执行工具，统一返回 ``ToolResult``，运行期失败一律不抛。"""
        start = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - start) * 1000.0

        tool = self._tools.get(name)
        if tool is None:
            # 模型会幻觉工具名：把它当正常情况，回一条带"可用清单"的错误让模型换招
            available = ", ".join(self._tools) or "(当前没有任何工具)"
            return ToolResult(
                tool_name=name,
                ok=False,
                error=f"未知工具 '{name}'。当前可用工具：{available}",
                duration_ms=elapsed(),
            )

        validator = _VALIDATOR_CLS(tool.parameters)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            location = ".".join(str(p) for p in first.absolute_path) or "(根对象)"
            return ToolResult(
                tool_name=name,
                ok=False,
                error=f"工具 '{name}' 参数不合法（字段 {location}）：{first.message}",
                duration_ms=elapsed(),
            )

        try:
            raw = await tool.run(args)
        except ToolError as exc:
            # 工具作者主动表达的业务错误：原样把人话原因回灌模型
            return ToolResult(tool_name=name, ok=False, error=str(exc), duration_ms=elapsed())
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都转成错误数据，不炸循环
            return ToolResult(
                tool_name=name,
                ok=False,
                error=f"工具 '{name}' 执行失败: {exc}",
                duration_ms=elapsed(),
            )

        # 归一化返回值：None -> {}，非 dict -> {"result": ...}，保证形状统一
        if raw is None:
            output: dict[str, Any] = {}
        elif not isinstance(raw, dict):
            output = {"result": raw}
        else:
            output = raw

        return ToolResult(tool_name=name, ok=True, output=output, duration_ms=elapsed())
