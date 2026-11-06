"""厂商无关的对话领域模型。

核心思想（学 Claude Code 的 content block）：一条 assistant 消息是**有序的内容块**
序列，"说"（TextBlock）和"做"（ToolCallBlock）是同一条消息里的不同块，
"边说边调工具"因此是数据模型的自然结果，不需要特殊分支。

千问 / OpenAI 的 "content 字符串 + tool_calls 数组" 形状，以及 Anthropic 原生的
交错块形状，都由各自适配层映射成本模块的块序列；运行时只认这里的模型。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ---------- 内容块 ----------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolCallBlock(BaseModel):
    # id 用于和工具结果严格配对；arguments 是适配层已把流式碎片拼全、解析好的 dict
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] = {}


Block = Annotated[TextBlock | ToolCallBlock, Field(discriminator="type")]


# ---------- 对话消息 ----------


class SystemMessage(BaseModel):
    role: Literal["system"] = "system"
    content: str


class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: str


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    blocks: list[Block]

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.blocks if isinstance(b, ToolCallBlock)]


class ToolMessage(BaseModel):
    """工具结果回灌。每个 ToolCallBlock 必须恰好对应一条（成功或失败都算）。"""

    role: Literal["tool"] = "tool"
    tool_call_id: str
    name: str
    content: str  # 成功为 JSON 字符串，失败为人话错误
    is_error: bool = False


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]


# ---------- 平台工具（云端执行）产物 ----------


class WebSource(BaseModel):
    """一次平台联网搜索引用的网页来源。

    千问 Responses 常常只给 URL、标题为空（标题由模型在正文里自行组织），故 title 可空。
    与本地工具产物 ToolResult 不同：这是云端 web_search 自己取回、只用于展示与引用的东西。
    """

    title: str | None = None
    url: str


# ---------- 用量与结束原因 ----------


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    # "命中缓存"的输入 token 数（公共前缀被复用、平台按折扣价计费的那部分）。
    # 界面靠它算这一轮 / 这个会话省了多少。
    # 厂商不一定给这个字段（老网关 / 测试假模型）→ 保持 None = 未知；
    # 0 是有意义的真值（一个都没命中），两者不能混。
    cached_tokens: int | None = None


class ToolResult(BaseModel):
    """一次工具执行的统一结果（错误也是数据）。

    ``ok=False`` 时 ``error`` 给出人话原因，由主循环交给会话层、回灌给模型，而不是抛异常
    终止循环。它和消息 / 用量一样是厂商无关的领域模型，因此放在这里，被 runtime 与 session
    共同引用，谁也不需要反向 import 谁。
    """

    tool_name: str
    ok: bool = True
    output: dict[str, Any] | None = None
    error: str | None = None
    duration_ms: float = Field(default=0.0, ge=0.0)


class FinishReason(StrEnum):
    FINAL_TEXT = "final_text"  # 模型不再调工具，给出最终答复
    MAX_ITERATIONS = "max_iterations"  # 触顶后摘掉工具强制总结
    CANCELLED = "cancelled"  # 用户主动取消
    FAILED = "failed"  # 循环级失败（如工具连续失败到阈值）
