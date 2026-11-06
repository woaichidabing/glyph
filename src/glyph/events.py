"""Agent 运行事件（loop → 外界）。

同一条事件流承担三件事：
1. 实时喂 UI（尤其 text_delta 逐字上屏）；
2. 以后写入会话日志做持久化 / 重放（状态事件才可序列化落盘）；
3. 事后统计成功率 / 耗时 / 迭代数。

因此事件分三类：
- **展示事件**（text_delta / reasoning_delta / tool_notice / web_search）：瞬时，只用于上屏，
  **不写入 messages、不落盘**；
- **瞬时控制事件**（turn_retry）：只通知 UI「本轮作废、重来一次」，表达的是过程而非一笔
  事实，**同样不落盘**（账本只存完整单元，重发的半截从不入账，重发本身无需留痕）；
- **状态事件**（其余）：代表一笔已确认的完整事实，可 json.dumps，用于入账 / 落盘。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .types import AssistantMessage, FinishReason, ToolCallBlock, Usage, WebSource


class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"


class TextDelta(BaseModel):
    """展示事件：模型吐出的增量文字，只上屏、不记账。"""

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ReasoningDelta(BaseModel):
    """展示事件：模型思考过程的增量（与正文是**两条流**），只上屏、不记账。

    性质同 WebSearchEvent：不进本轮 assistant blocks、不写账本、永不进 messages()
    （会话层已定 reasoning 不进模型视图），所以思考再长也不占上下文预算。
    前端把它折成默认收起的「思考块」；落盘另走会话层的 reasoning 流水，
    与事件流各记各的（落盘在 assistant 收完整时整条记一次，不做增量落盘）。
    """

    type: Literal["reasoning_delta"] = "reasoning_delta"
    delta: str


class ToolNoticeEvent(BaseModel):
    """展示事件：模型没有先说话时，给用户一个稳定的工具执行前提示。"""

    type: Literal["tool_notice"] = "tool_notice"
    message: str = "我先处理一下，再把结果告诉你。"


class AssistantMessageEvent(BaseModel):
    """状态事件：一整条 assistant 消息收完整后才发（delta 不进账本）。"""

    type: Literal["assistant_message"] = "assistant_message"
    message: AssistantMessage


class ToolCallEvent(BaseModel):
    """状态事件：宣告一个工具开始执行（参数已完整）。"""

    type: Literal["tool_call"] = "tool_call"
    call: ToolCallBlock


class ToolResultEvent(BaseModel):
    """状态事件：一个工具执行结束（成功或失败都发，和 call.id 严格配对）。"""

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    name: str
    ok: bool
    duration_ms: float = 0.0
    output: dict | None = None
    error: str | None = None


class WebSearchEvent(BaseModel):
    """展示事件：平台内置 web_search 的进度（云端执行，只用于过程可见）。

    与 ToolCallEvent/ToolResultEvent 的区别：没有本地执行、不需要把结果回灌给模型；
    search_id 只供前端把同一次搜索的开始/完成精确配对（并行搜索不串台），不是要
    回灌的 call_id。前端据此渲染"🔍 正在搜 / 搜到了哪些来源链接"。不进上下文账本，
    语义同思考过程——只展示。
    """

    type: Literal["web_search"] = "web_search"
    phase: Literal[
        "started", "in_progress", "searching", "completed", "no_result"
    ]
    search_id: str | None = None
    query: str | None = None
    sources: list[WebSource] = []


class TurnRetryEvent(BaseModel):
    """状态事件：当前模型轮即将整体重发（网络中断 / 流式空闲超时 / 输出截断升档）。

    语义是"本轮重来"：本轮此前下发的 text_delta / tool_call 一律作废（这轮 assistant
    尚未入账，账本本身是干净的）。UI 收到后应回退当前这条**尚未确认**的 assistant 气泡
    （清掉已上屏的半截字、移除未确认的工具行），再接收重新推送的内容。

    reason 取值：``network`` 流中途断连 / 报错；``idle_timeout`` 连接还在但长时间一个字
    都不吐（服务端卡住）；``max_tokens`` 输出被截断、静默升档重发；``context_too_long``
    §10 上下文超长、先减重（§10 emergency_view）再重发；``model_fallback`` 本轮零产出
    就撞额度 / 鉴权错误、已临时换一个兜底脑（默认 kimi-k3）重发，message 说明换成了谁。
    """

    type: Literal["turn_retry"] = "turn_retry"
    reason: Literal[
        "network", "idle_timeout", "max_tokens", "context_too_long", "model_fallback"
    ]
    attempt: int  # 第几次重发，从 1 开始
    message: str = ""


class ResultEvent(BaseModel):
    """状态事件：一次 run 的终态。"""

    type: Literal["result"] = "result"
    text: str
    finish_reason: FinishReason
    iterations: int
    usage: Usage | None = None
    # 本次 run 平台联网搜索总次数（按次计费，独立于 token；来自 usage.x_tools 计数）
    web_search_count: int = 0
    # 上下文用量环：最后一轮实际发送给模型的 input_tokens（随对话累积到最大，≈当前
    # 上下文大小）。区别于上面逐轮累加的 usage（那是计费口径）；模型没回 usage 时为
    # None，环显示"未知"而不是谎报 0。
    context_input_tokens: int | None = None
    # 当前大脑的上下文窗口（环的分母），取自模型能力表；未知 / 测试假模型回退保守默认，
    # 恒为正数，保证环不除零。
    context_window: int = 0


class CompactionEvent(BaseModel):
    """状态事件：上下文自动压缩的进度（L2 装箱 / L3 浓缩 / 413 应急），全程对用户透明。

    时序铁律（设计稿 §13 · C9）：判出 L3 后，``started`` 必须在**发起写手请求之前**发出
    ——写手可能慢 / 卡 / 超时，若等拿到摘要才提示，用户会以为助手死机。事件链：
    ``started`` →（材料对半砍重试时 ``retrying``）→ ``commit`` 成功 ``done``；
    任何失败 / 降级发 ``failed`` / ``skipped``。

    - ``phase``：started（开始浓缩，写手请求前）/ l2（仅 L2 装箱，轻提示）/
      retrying（材料偏大，已自动减半后重试写手）/ done（压缩成功落账）/
      failed（尝试了但失败）/ skipped（本轮不压、全量继续，如没配写手）。
    - ``level``："l2" / "l3" / "l2+l3"，无压缩时为空串。
    - ``before_tokens / after_tokens``：触发时预判水位与压后水位（遥测 D2 + 状态行）。
    - ``material_truncated``：写手材料是否被 §9.3 预算 / 对半砍截断（影响摘要完整度提示）。
    """

    type: Literal["compaction"] = "compaction"
    phase: Literal["started", "l2", "l3", "retrying", "done", "failed", "skipped"]
    level: str = ""
    message: str = ""
    before_tokens: int | None = None
    after_tokens: int | None = None
    boundary_id: str | None = None
    material_truncated: bool = False
    # [v2.1 · D2] 写手耗时（毫秒），仅 done/failed 携带，供状态行与遥测曲线。
    elapsed_ms: float | None = None


class ErrorEvent(BaseModel):
    """状态事件：未预期的循环级错误（受控失败用 ResultEvent(failed) 表达）。"""

    type: Literal["error"] = "error"
    message: str


AgentEvent = Annotated[
    RunStarted
    | TextDelta
    | ReasoningDelta
    | ToolNoticeEvent
    | AssistantMessageEvent
    | ToolCallEvent
    | ToolResultEvent
    | WebSearchEvent
    | TurnRetryEvent
    | CompactionEvent
    | ResultEvent
    | ErrorEvent,
    Field(discriminator="type"),
]
