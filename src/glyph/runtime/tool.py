"""工具协议与工具执行结果。

业务无关：任何希望被模型调用的能力都实现 ``Tool`` 协议并注册到 ``ToolRegistry``。
运行时不关心工具具体做什么（批改 / OCR / 检索都一样），只认 name / description /
parameters / run 这四样。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from ..types import ToolResult

__all__ = ["Tool", "ToolResult"]


class Tool(ABC):
    """所有工具的统一协议。

    子类以类属性提供 ``name`` / ``description`` / ``parameters``，并实现 ``run``。
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema，描述 run 需要的参数
    # 只读 / 写入身份（R10）：默认 False —— 未声明一律保守按"写入"处理，
    # 含写入工具的批次整批串行执行；只读工具继承 tools.base.ReadOnlyTool。
    readonly: ClassVar[bool] = False
    # 交互式身份：默认 False —— 声明为 True 的工具（典型是"问用户一句"）不受
    # ``tool_timeout`` 管，因为它要等人，人多久才点一下不是程序能猜的。
    # 未声明一律按普通工具处理，绝不能因为漏声明就免掉超时。
    interactive: ClassVar[bool] = False

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        """执行工具，返回可 JSON 序列化的 dict。

        业务上"没做成"应抛 ``ToolError``（见 errors.py）；注册表会把它转成
        一条错误结果回灌给模型，而不是让异常炸穿循环。
        """


# ``ToolResult`` 已上移到领域模型 ``glyph.types``（runtime 与 session 共用同一份），
# 这里 re-export 仅为兼容旧导入路径 ``from glyph.runtime.tool import ToolResult``。
