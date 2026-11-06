"""LLM 协议（厂商无关）与"适配层 → loop"的流式事件。

关键边界：厂商 SSE 的细碎报文（文本碎片、按 index 分片的工具参数 JSON、平台工具状态）
由具体适配层（如 qwen.py / responses.py）在内部拼全，loop 只消费这里规整后的语义事件
——半截工具参数 JSON 绝不进入 loop。

请求是无状态的：每次 stream_chat 都带上完整 messages 与工具清单（对齐
Codex 刻意不用 previous_response_id 的做法），换来可换厂商、可重放、好压缩。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..errors import GlyphError
from ..types import Message, ToolCallBlock, Usage, WebSource


class TextDelta(BaseModel):
    """模型吐出的增量文本（展示用）。"""

    type: Literal["llm_text_delta"] = "llm_text_delta"
    delta: str


class ReasoningDelta(BaseModel):
    """模型的思考过程增量（**展示型**，与 WebSearchDelta 同一性质）。

    两个关键边界：
    1. **不进本轮 blocks、不写账本**——它是过程不是一笔事实；会话层已定
       "reasoning 永远不进 messages()"（见 `session/records.py::ReasoningPayload`），
       所以它再长也不占上下文预算；
    2. 与 TextDelta 是**两条流**，绝不合并：思考是模型的自言自语，正文才是它要说的话。
       合并会直接把内心话当答复上屏。

    厂商字段名不同：Chat 通道读 `delta.reasoning_content`，Responses 通道读
    `response.reasoning_text.delta`（适配层各自翻译，loop 只见本事件）。
    """

    type: Literal["llm_reasoning_delta"] = "llm_reasoning_delta"
    delta: str


class ToolCallReady(BaseModel):
    """一个参数已拼全、可执行的【本地】工具调用。"""

    type: Literal["llm_tool_call_ready"] = "llm_tool_call_ready"
    call: ToolCallBlock


class WebSearchDelta(BaseModel):
    """平台内置 web_search 的一次状态更新（云端执行，后端不跑、也不回灌工具结果）。

    与 ToolCallReady 的本质区别：它只用于"过程可见"（正在搜 / 搜到了哪些来源），
    主循环不得把它送去注册表执行，也不产生需要配对的 function_call_output；
    它不进发给模型的上下文（阶段 1），像思考过程一样是"展示型"事件。
    """

    type: Literal["llm_web_search"] = "llm_web_search"
    # started：output_item.added 搜索项刚建立（带 search_id）；in_progress/searching：
    # 平台空壳进度帧（无 id）；completed：output_item.done 聚合完成且带来源（带 search_id）；
    # no_result：流程走完却没有任何来源（=没搜到），前端显失败、模型不许据此编造。
    phase: Literal["started", "in_progress", "searching", "completed", "no_result"]
    # 搜索项 id（作用同 function 的 call_id）：started/completed 带上，前端据此精确
    # 配对，并行多个搜索乱序完成时不会把 A 的来源错挂到 B；空壳进度帧为 None。
    search_id: str | None = None
    query: str | None = None
    sources: list[WebSource] = []


class TurnEnd(BaseModel):
    """本轮模型输出结束。

    stop_reason：stop=给最终答复，tool_calls=要执行工具，
    length=输出撞上 max_output_tokens 上限被截断（话没说完 / 工具参数没生成完）。
    web_search_count：本轮平台搜索次数（计费计数，来自 usage.x_tools），未知为 None。
    """

    type: Literal["llm_turn_end"] = "llm_turn_end"
    stop_reason: Literal["stop", "tool_calls", "length"]
    usage: Usage | None = None
    web_search_count: int | None = None


LLMStreamEvent = Annotated[
    TextDelta | ReasoningDelta | ToolCallReady | WebSearchDelta | TurnEnd,
    Field(discriminator="type"),
]


class LLMClient:
    """LLM 适配层协议。

    具体实现：ScriptedFakeStreamingLLM（测试）、QwenLLM / ResponsesLLM（真千问）。

    ``strip_tools=True``：**连平台内置工具一起摘掉**（主循环的续写轮 / 触顶总结轮）。
    它与 ``tools=[]`` 是两件事 —— ``[]`` 只说明"这一轮没有本地工具"（平台工具该开还得开），
    ``strip_tools=True`` 才是"这一轮什么都不许用，只动嘴"（见 T02）。
    """

    def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict],
        max_tokens: int | None = None,
        strip_tools: bool = False,
        model: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:  # pragma: no cover - 协议声明
        raise NotImplementedError


class LLMError(GlyphError):
    """调模型失败。

    retryable=True：网络抖动 / 5xx / 429。谁负责重试要分两段看：
      - 建连阶段（流还没开始）：适配层自己有限次退避重试，用户感觉不到；
      - 流已经开始之后：适配层**不重试**（否则会重复推送已上屏的 delta），
        把 retryable 标记交给主循环决定要不要整轮重发（半截未入账，故幂等）。
    retryable=False：4xx / 鉴权等，重试无意义，直接失败。

    这个 True/False 由 markr/retry.py::is_retryable 单点判定（R1）。

    quota_or_auth：原始异常是不是"这个模型用不了"（没额度 / 欠费 / 没权限 / 钥匙不对）。
    适配层包装异常时状态码就丢了，主循环再想判断只能看一段文案；所以在**还有原始异常**
    的那一刻按 markr/retry.py::is_quota_or_auth_error 算好、带着走。判定单点仍在那一个
    函数里，这里只是把它算出来的结论运过去。
    """

    def __init__(self, message: str, *, retryable: bool, quota_or_auth: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.quota_or_auth = quota_or_auth


class ContextTooLongError(LLMError):
    """上下文超长（HTTP 413 / ``context_length_exceeded`` 一类，§10 应急触发源）。

    与普通网络错误的本质区别：**重发同样的消息没有意义**（一个字没少，还是超长），
    必须先由会话层减重（保最近往返 + 提货单、不砍未结束任务原文）再重发。因此固定
    ``retryable=False``——它不走 inner-while 的网络重试分支，而走 §10 的"先减重、再
    重发"应急分支。归类单点在 :func:`glyph.retry.is_context_too_long`。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
