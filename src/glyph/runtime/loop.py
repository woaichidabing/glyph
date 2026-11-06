"""AgentLoop：业务无关的 Tool-use 主循环（流式）。

每一轮：可取消地流式消费模型输出（文本碎片逐字转发、工具调用收集）→ 轮结束把整条
assistant 消息**一次性**记进会话历史（delta 只展示、不记账）→ 没有工具调用就给终态；
有工具调用则并行执行并把结果回灌，进入下一轮，直到模型不再调工具 / 触顶 / 取消 / 失败。

**账本不在 loop 里**。循环不持有消息数组、不碰文件，只通过构造注入的 ``HistoryPort``
（见 ``history_port.py``）做两件事：要发什么问 ``messages()``，刚发生什么调 ``record_*()``。
具体落盘 / 重建 / 将来的上下文压缩都在会话层，换一个 history 对象即可，loop 一行不改。

协作式取消（``cancel()``）：取消信号随时可置位，循环在"模型流静默 / 流事件之间 /
工具批次执行中 / 每轮开头"这些点响应；在飞的模型流与工具协程会被立刻掐断，但历史只
按完整单元提交 —— 半截 assistant 消息丢弃，已开始的工具调用一律闭合为一条"已取消"
结果，保证每个 tool_call 都有配对结果、history 停在完整边界、可据此开新 run。

取消不是"请求"而是"通知"：工具收到取消信号后有一段宽限期（``cancel_grace_period``）
自行收摊，到期仍不退出的（同步阻塞、或吞掉取消信号的实现）不再等它 —— 主循环直接
补结果收尾。同理单个工具也有墙钟上限（``tool_timeout``），超时即补一条失败结果，
把决定权交回模型 —— 唯一例外是声明了 ``interactive`` 的工具（"问用户一句"），它要等人，
给它设上限等于在用户还没看到问题时就把这一轮判死。两条都是为了同一个目的：
**没有任何东西能把一轮永久挂住**（交互式工具的"挂住"由用户自己按停止收口）。

模型侧错误（鉴权失败 / 网络耗尽重试等，适配层包成 LLMError）不会让 run 抛 traceback，
而是发一条 error 事件并以 failed 终态收尾 —— 错误即数据，循环本身尽量不把异常甩给 UI。

循环本身不含任何批改等业务逻辑（铁律 R7），业务能力都是注册进来的工具。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from ..config import CONTEXT_MODE_STANDARD, DEFAULT_MAIN_FALLBACK_MODELS
from ..context import CONTINUATION_PROMPT
from ..errors import GlyphError
from ..events import (
    AgentEvent,
    AssistantMessageEvent,
    CompactionEvent,
    ErrorEvent,
    ResultEvent,
    RunStarted,
    ToolCallEvent,
    ToolNoticeEvent,
    ToolResultEvent,
    TurnRetryEvent,
    WebSearchEvent,
)
from ..events import (
    ReasoningDelta as ReasoningDeltaEvent,
    TextDelta as TextDeltaEvent,
)
from ..llm import base as llm_base
from ..llm.models import (
    CONTEXT_MODE_MAX,
    CONTEXT_WINDOW_MAX,
    CONTEXT_WINDOW_STANDARD,
    context_window_of,
)
from ..observability import summarize_args
from ..retry import is_quota_or_auth_error
from ..types import (
    AssistantMessage,
    FinishReason,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    Usage,
)
from .compaction_metrics import CompactionMetric
from .history_port import HistoryPort
from .registry import ToolRegistry
from .sink import EventSink

if TYPE_CHECKING:  # runtime 不 import session（B2）；写手属 llm，仅类型检查时引用
    from ..llm.compactor import CompactionSummarizer

_CANCELLED_TEXT = "任务已被用户取消。"

# §10 被动应急两档都耗尽时的兜底人话（正常应使用 session EmergencyView.reason；
# 仅当连一次可行视图都没拿到时用它，runtime 不 import session、故在此自备同义文案）。
_EMERGENCY_GIVE_UP_TEXT = (
    "这段对话太长了，我已经没法在不丢失内容的前提下继续。请新开一个会话再发；你之前的"
    "批改成品与定稿都完整保存在会话记录里，不会丢。"
)

# 主脑模型额度 / 鉴权耗尽、且不满足"零产出自动换兜底脑"条件时（已吐字 / 已调工具 /
# 已联网，或兜底脑也用不了）的终态人话：明确指路点顶部模型名换一个，或去设置查 API Key。
_QUOTA_GIVE_UP_TEXT = (
    "这个模型现在用不了：账号里可能没开通，或者余额/额度已经用完。"
    "到模型服务商的控制台开通或充值后再试；急着用的话，点顶部的模型名先换一个。"
)

# 压缩 async generator 里表达"写手请求期间被取消"的哨兵事件（不是真事件，不上屏）
_COMPACTION_CANCELLED = object()

logger = logging.getLogger("glyph.runtime")


# ---------- 正常路径打点（设计稿 §2.1-A/B） ----------
# 这三行是本模块唯一的日志拼装点，样式统一为 key=value 空格分隔、**单行**、便于 grep。
# 刻意都写成小函数而不是散在各处的 f-string —— 字段增删只改一处。
# 脱敏规则（正文只报长度）集中在 observability.summarize_args 那一处，不在这里各判一遍。


def _log_turn_end(
    iteration: int,
    stop_reason: str | None,
    usage: Usage | None,
    tool_count: int,
    elapsed_ms: float,
) -> None:
    """每轮一条：循环转了几轮、为什么停、花了多少 token、用了几秒。"""
    logger.info(
        "轮结束 iter=%d stop=%s in=%s out=%s cached=%s tools=%d 用时=%.1fs",
        iteration,
        stop_reason or "?",
        usage.input_tokens if usage else "?",
        usage.output_tokens if usage else "?",
        (usage.cached_tokens if usage and usage.cached_tokens is not None else "?"),
        tool_count,
        elapsed_ms / 1000.0,
    )


def _log_run_end(
    reason: FinishReason,
    iters: int,
    usage: Usage | None,
    web_search_count: int,
    elapsed_ms: float,
) -> None:
    """运行终态一条：与每轮那行一起夹出完整时间轴。"""
    logger.info(
        "运行结束 reason=%s iters=%d in=%s out=%s 搜索=%d 用时=%.1fs",
        reason.value,
        iters,
        usage.input_tokens if usage else "?",
        usage.output_tokens if usage else "?",
        web_search_count,
        elapsed_ms / 1000.0,
    )


def _log_tool_result(name: str, ok: bool, duration_ms: float | None, note: str) -> None:
    """每个工具一条：名字 / 成功失败 / 耗时 / 参数摘要。**摘要里没有正文**。"""
    logger.info(
        "工具 %s %s %.2fs%s",
        name,
        "完成 ok=True" if ok else "失败 ok=False",
        (duration_ms or 0.0) / 1000.0,
        f" {note}" if note else "",
    )


def _swallow_task_exception(task: asyncio.Task) -> None:
    """遗孤任务的收尸回调：把异常取走，免得 asyncio 在回收时刷 "never retrieved"。

    只在"取消后工具仍不退、主循环放弃等待"这条路径上用到 —— 我们不再关心它的结局，
    但不能让它在事件循环里留下一具会报警告的尸体。
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:  # pragma: no cover - 兜底，常态下走到这里都是被取消
        logger.debug("被放弃的工具任务最终以异常结束：%r", exc)


class AgentLoop:
    def __init__(
        self,
        llm,
        registry: ToolRegistry,
        *,
        history: HistoryPort,
        context_mode: str = CONTEXT_MODE_STANDARD,
        main_fallback_models: tuple[str, ...] | None = None,
        max_iterations: int = 15,
        max_consecutive_tool_failures: int = 3,
        max_stream_retries: int = 2,
        retry_backoff_base: float = 0.5,
        max_output_tokens: int = 8192,
        max_output_tokens_upgrade: int = 65536,
        max_continuations: int = 1,
        stream_idle_timeout: float = 90.0,
        tool_timeout: float = 120.0,
        cancel_grace_period: float = 2.0,
        compactor: CompactionSummarizer | None = None,
        compaction_observer: Callable[[CompactionMetric], None] | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        # 唯一对话账本由会话层持有：loop 只用它、不管它怎么存（构造注入 HistoryPort）
        self.history = history
        # 最大上下文档位（standard 30 万 / max 100 万）：只影响环分母与压缩阈值，
        # 由装配层从 settings 注入；默认标准档，测试直接构造 loop 时可不传。
        self.context_mode = context_mode
        # 主脑本轮零产出就撞额度/鉴权时的临时兜底脑（默认 kimi-k3）；None 用配置默认。
        # 只在单次 run 内临时换、不写回 settings / 用户持久选择的模型。
        self.main_fallback_models = (
            tuple(main_fallback_models)
            if main_fallback_models is not None
            else DEFAULT_MAIN_FALLBACK_MODELS
        )
        self.max_iterations = max_iterations
        self.max_consecutive_tool_failures = max_consecutive_tool_failures
        # 流开始后断网（可重试错误）时，当前轮整体重发的最多次数与指数退避基数
        self.max_stream_retries = max_stream_retries
        self.retry_backoff_base = retry_backoff_base
        # 单轮输出上限：先用基础值，输出被截断（length）时静默升到升级值重发当前轮
        self.max_output_tokens = max_output_tokens
        self.max_output_tokens_upgrade = max_output_tokens_upgrade
        # 升到上限仍截断时，最多续给下一轮几次（防"永远说不完"的死循环）
        self.max_continuations = max_continuations
        # 流式空闲超时：相邻两个事件之间静默超过该秒数，即视为"连接还在但服务端卡住"，
        # 按可重试错误重发当前轮；<=0 表示禁用（交回 SDK / 系统的超时）。
        self.stream_idle_timeout = stream_idle_timeout if stream_idle_timeout > 0 else None
        # 单个工具的墙钟上限（秒）：超时就补一条失败结果、把决定权交回模型，绝不干等。
        # <=0 表示不设上限（交回工具自己保证）。
        self.tool_timeout = tool_timeout if tool_timeout and tool_timeout > 0 else None
        # 取消后的宽限期（秒）：cancel 信号发出后，给工具这么久自行收摊；
        # 到期仍不退出的按"卡死"处理 —— 不再等它，直接补结果收尾（见 _execute_tool_batch）。
        # 不进 config：这是循环内部的安全阀，不是给用户调的旋钮。
        self.cancel_grace_period = max(cancel_grace_period, 0.0)
        # L3 上下文浓缩用的**便宜写手**（如 qwen3.7-flash），由装配根注入；history 不碰网络
        # （B2 铁律），写手只在 loop 手里。None 时判出 L3 也只能发 skipped、全量继续。
        self.compactor = compactor
        # [v2.1 · D2] 压缩遥测旁路观察者（如 jsonl 落盘）；只收 runtime DTO、可为 None，
        # 任何观察者异常都被吞掉，遥测绝不影响主循环。
        self.compaction_observer = compaction_observer
        self._cancel = asyncio.Event()
        # A2 上下文水位记账：**跨 run() 保持**（用户多轮对话是多次 run()，每次 run 开头
        # 要以上一轮服务端回报的真实 input/output 判级，否则每轮都被当成"首轮只做 L1"，
        # 主动 L2/L3 在真实多轮会话里永不触发）。会话切换会重建 loop，自然归零。
        self._last_context_input: int | None = None
        self._last_context_output: int | None = None
        # 同一 loop 的 run 串行化：忙时后来的请求排队等锁，杜绝并发写花同一本账
        self._run_lock = asyncio.Lock()

    def cancel(self) -> None:
        """请求取消。信号立即置位，在飞的模型流 / 工具协程会被尽快掐断。"""
        self._cancel.set()

    @property
    def busy(self) -> bool:
        """是否有 run 正在执行（持锁段）。

        忙时新来的请求会在锁上 FIFO 排队；服务层 / UI 据此显示"排队中"，而不是误判卡死。
        """
        return self._run_lock.locked()

    def _effective_window(self) -> int:
        """当前大脑在当前档位下的有效上下文窗口（压缩阈值的分母）。

        真实 llm 带 model；测试假模型没有则回退保守默认（与终态环分母同一口径）。
        """
        # 能力只认清单（2026-10-29）：用户/内置表给当前模型填了标称窗口，就按它算；
        # 没填才回落到"能力表 / 保守兜底 + 档位上限"的老口径。
        declared = int(getattr(self.llm, "model_context_window", 0) or 0)
        if declared > 0:
            cap = CONTEXT_WINDOW_MAX if self.context_mode == CONTEXT_MODE_MAX else CONTEXT_WINDOW_STANDARD
            return max(1, min(declared, cap))
        return context_window_of(getattr(self.llm, "model", None), self.context_mode)

    @contextlib.asynccontextmanager
    async def _cancel_waiter(self):
        """提供一个"取消信号等待任务"，退出时确保取消，避免任务泄漏（DRY 取消竞争）。"""
        waiter = asyncio.ensure_future(self._cancel.wait())
        try:
            yield waiter
        finally:
            waiter.cancel()

    async def run(
        self, user_input: str, *, sink: EventSink | None = None
    ) -> AsyncIterator[AgentEvent]:
        # 串行化：同一时刻只允许一个 run 操作这本账，后来者在锁上 FIFO 排队，
        # 等前一个彻底收尾（正常 / 取消 / 失败都释放锁）后再开始，杜绝并发写花历史。
        async with self._run_lock:
            try:
                async for event in self._run_impl(user_input, sink=sink):
                    yield event
            except (GlyphError, OSError) as exc:
                # 落盘失败（磁盘满 / 权限 / 会话已关闭）也属于"错误即数据"：
                # 不许裸崩——那会让前端停在"生成中…"永远转圈、已生成的回答全丢。
                # 这里不能再调 history 收尾（它正是坏掉的那个），只发终态事件。
                logger.exception("本轮运行因会话层错误中断：%s", exc)
                yield ErrorEvent(message=f"这一轮没能记进会话：{exc}")
                yield ResultEvent(
                    text="",
                    finish_reason=FinishReason.FAILED,
                    iterations=0,
                    usage=None,
                )

    async def _run_impl(
        self, user_input: str, *, sink: EventSink | None = None
    ) -> AsyncIterator[AgentEvent]:
        async def emit(event: AgentEvent) -> AgentEvent:
            if sink is not None:
                try:
                    await sink.on_event(event)
                except Exception as exc:  # noqa: BLE001 - 展示层故障不得反杀任务
                    # 只记录、不静默：UI / SSE 崩了或序列化失败，后台 run 仍要把活干完
                    logger.warning("sink.on_event 处理 %s 失败，已忽略: %s", event.type, exc)
            return event

        start = time.perf_counter()
        total_in = 0
        total_out = 0
        total_cached = 0
        total_web_searches = 0
        saw_usage = False
        # 与 saw_usage 分开：模型可能给了总量却没给缓存明细，那时缓存仍是"未知"而非 0
        saw_cached = False
        # 上下文水位（A2）用实例属性 self._last_context_input / _output：它们必须**跨 run()
        # 保持**，下一次用户对话开头才能以上一轮服务端回报的真实用量判级（见 __init__）。
        # 本次 run 内每收到一次 TurnEnd 就覆盖，循环结束留下最后一轮（= 当前全量大小）。

        def total_usage_event() -> Usage | None:
            """逐轮累加的总用量；模型从没给过 usage 时为 None（不报 0，保持"未知"语义）。"""
            if not saw_usage:
                return None
            return Usage(
                input_tokens=total_in,
                output_tokens=total_out,
                cached_tokens=total_cached if saw_cached else None,
            )

        async def finish(reason: FinishReason, iteration: int, text: str) -> ResultEvent:
            """统一收尾：先把终态（含取消）记进 history，再产出 result 事件。"""
            iters = (
                self.max_iterations
                if reason is FinishReason.MAX_ITERATIONS
                else min(iteration, self.max_iterations)
            )
            if reason is FinishReason.CANCELLED:
                await self.history.record_cancel(reason="user", iteration=iteration)
            elapsed = (time.perf_counter() - start) * 1000.0
            await self.history.record_turn_end(
                reason=reason,
                duration_ms=elapsed,
                usage=total_usage_event(),
                context_input_tokens=self._last_context_input,
                iterations=iters,
            )
            # 运行终态一行（正常路径打点，设计稿 §2.1-A）：reason / iters / tokens / 用时。
            # 放在 finish() 里而不是各条 return 前 —— 所有终态（正常/取消/失败/触顶）
            # 都经过这个函数，一处打点就不会漏掉某条分支。
            _log_run_end(reason, iters, total_usage_event(), total_web_searches, elapsed)
            return ResultEvent(
                text=text,
                finish_reason=reason,
                iterations=iters,
                usage=total_usage_event(),
                web_search_count=total_web_searches,
                context_input_tokens=self._last_context_input,
                # 分母按当前大脑型号取（真实 llm 带 model；测试假模型没有则回退保守默认）
                context_window=self._effective_window(),
            )

        self._cancel.clear()  # 同一 loop 可在取消后基于 history 开新 run
        await self.history.record_user_message(user_input)
        yield await emit(RunStarted())

        consecutive_failures = 0
        iteration = 0
        forcing_summary = False
        continuations = 0
        answer_parts: list[str] = []
        force_text_turn = False  # 续写轮：摘工具、直接用升级额度，逼模型只动嘴接着说
        # 有效窗口整次 run 不变（model / 档位固定），算一次供每轮压缩决策使用
        effective_window = self._effective_window()
        # 主脑额度兜底：首选脑名（换脑提示文案用）、待消耗的兜底链、本次 run 当前生效脑。
        # active_model=None 表示用 llm 构造时绑定的首选脑；一旦换过，本 run 后续轮沿用兜底脑
        # （首选脑已确认没额度），但不写回用户选择，下次 run 仍从首选脑开始、再快速兜底。
        primary_model = getattr(self.llm, "model", None)
        fallback_chain = [m for m in self.main_fallback_models if m and m != primary_model]
        active_model: str | None = None

        while True:
            iteration += 1
            # 本轮读数打点用的两个局部量：轮首计时 + 本轮（模型这一次响应）的用量。
            # 与 total_* 分开——total 是整次 run 的计费累加，这两个只描述「这一轮」。
            turn_started = time.perf_counter()
            turn_usage_last: Usage | None = None

            # 安全检查点：每轮开头
            if self._cancel.is_set():
                yield await emit(await finish(FinishReason.CANCELLED, iteration, _CANCELLED_TEXT))
                return

            # 上下文压缩（§12.2/§13）：每轮开头、进入网络重试 inner-while **之前**决策一次。
            # inner-while 的断连重发不重跑压缩（重发幂等）；只有 §10 的 413 应急才先减重再重发。
            # comp_state 在每轮重置，用来把主动 blocked 的应急视图 / 致命不可行传出压缩步骤。
            comp_state: dict = {}
            async for comp_event in self._run_compaction(
                effective_window=effective_window,
                last_input=self._last_context_input,
                last_output=self._last_context_output,
                state=comp_state,
            ):
                if comp_event is _COMPACTION_CANCELLED:
                    # 写手请求期间被取消：掐断、不落 boundary，按取消收尾
                    yield await emit(
                        await finish(FinishReason.CANCELLED, iteration, _CANCELLED_TEXT)
                    )
                    return
                yield await emit(comp_event)

            # 主动 blocked 且应急也组不出干净请求（B5 单篇超长 / 整体过长）：明确失败给人话
            if comp_state.get("emergency_fatal"):
                fatal_message = comp_state["emergency_fatal"]
                yield await emit(ErrorEvent(message=fatal_message))
                yield await emit(await finish(FinishReason.FAILED, iteration, fatal_message))
                return

            # 主动应急已减重时，本轮 inner-while 用应急视图替换常规 messages；被动 413 时
            # 这个变量会在错误分支里被再次替换。None = 照常取 history.messages()。
            emergency_messages: list | None = comp_state.get("emergency_messages")
            emergency_strength: int | None = comp_state.get("emergency_strength")

            # 强制总结轮 / 续写轮摘掉工具，逼模型用文字收尾；其余轮带全量工具
            strip_tools = forcing_summary or force_text_turn
            tools = [] if strip_tools else self.registry.schemas()
            force_text_turn = False  # 仅作用于本轮

            # ---- 消费一轮模型流；网络中断可安全重发当前轮 ----
            # 本轮 assistant 要等流完整结束才入账，所以重发前账本是干净的、重发幂等；
            # 已经上屏的半截 delta 靠 turn_retry 事件通知 UI 回退。
            stream_attempt = 0
            # 普通轮从基础额度开始、截断才升档；续写轮直接用升级额度（它本就为长文兜底）
            current_max_tokens = (
                self.max_output_tokens_upgrade if strip_tools else self.max_output_tokens
            )
            upgraded = strip_tools
            while True:
                # 展示提示只属于当前模型尝试；重试会回退未确认事件，下一次尝试需要重新提示。
                turn_notice_emitted = False
                # strip_tools：摘工具轮（续写 / 触顶总结）要把"平台内置工具"也一起摘掉，
                # 光靠 tools=[] 表达不了 —— [] 也可能是"这轮本来就没本地工具"的纯搜索轮。
                agen = self.llm.stream_chat(
                    emergency_messages
                    if emergency_messages is not None
                    else self.history.messages(),
                    tools,
                    max_tokens=current_max_tokens,
                    strip_tools=strip_tools,
                    model=active_model,
                )
                blocks: list = []
                tool_calls: list[ToolCallBlock] = []
                # 本尝试的思考原文：转发上屏的同时攒着，assistant 收完整时整条落盘；
                # 尝试失败 / 本轮重发时随半截一起丢弃（不落盘，见 record_reasoning）
                reasoning_parts: list[str] = []
                stream_cancelled = False
                stream_error: str | None = None
                stream_error_retryable = False
                # §10：上下文超长（ContextTooLongError）与网络抖动分开走——先减重再重发
                stream_error_context_too_long = False
                stream_error_quota = False
                stream_retry_reason = "network"
                turn_stop_reason: str | None = None
                attempt_had_text = False
                try:
                    async with self._cancel_waiter() as cancel_wait:
                        while True:
                            nxt = asyncio.ensure_future(agen.__anext__())
                            done, _ = await asyncio.wait(
                                {nxt, cancel_wait},
                                return_when=asyncio.FIRST_COMPLETED,
                                # 相邻两个事件之间的静默上限：每收到一个事件就重新等待，
                                # 因此窗口逐事件重置，只掐"一个字都不吐"，不误伤慢而活跃的流
                                timeout=self.stream_idle_timeout,
                            )
                            if cancel_wait in done:
                                # 模型长时间静默时取消：丢弃还没拿到的半个事件
                                if not nxt.done():
                                    nxt.cancel()
                                await asyncio.gather(nxt, return_exceptions=True)
                                stream_cancelled = True
                                break
                            if nxt not in done:
                                # 既无新事件也无取消：阈值内一个字都没吐（连接还在、服务端
                                # 卡住）。掐断挂起的取数，按可重试错误处理，复用本轮重发。
                                if not nxt.done():
                                    nxt.cancel()
                                await asyncio.gather(nxt, return_exceptions=True)
                                stream_error = (
                                    f"模型已连续 {self.stream_idle_timeout:.0f} 秒没有任何输出，"
                                    "疑似服务端卡住"
                                )
                                stream_error_retryable = True
                                stream_retry_reason = "idle_timeout"
                                break
                            try:
                                event = nxt.result()
                            except StopAsyncIteration:
                                break
                            except Exception as exc:  # noqa: BLE001 - 归类后决定重试或终态
                                stream_error = str(exc) or exc.__class__.__name__
                                stream_error_retryable = (
                                    isinstance(exc, llm_base.LLMError) and exc.retryable
                                )
                                stream_error_context_too_long = isinstance(
                                    exc, llm_base.ContextTooLongError
                                )
                                stream_error_quota = is_quota_or_auth_error(exc)
                                break

                            if isinstance(event, llm_base.TextDelta):
                                # 展示事件：逐字转发，只累积到本轮块，不写账本
                                yield await emit(TextDeltaEvent(delta=event.delta))
                                if event.delta.strip():
                                    attempt_had_text = True
                                if blocks and isinstance(blocks[-1], TextBlock):
                                    blocks[-1] = TextBlock(text=blocks[-1].text + event.delta)
                                else:
                                    blocks.append(TextBlock(text=event.delta))
                            elif isinstance(event, llm_base.ReasoningDelta):
                                # 展示事件：模型的思考过程（与正文两条流），转发上屏 + 攒着
                                # 待本轮收完整时落盘。不进 blocks / 不进给模型的账本，也不算
                                # "先说过话"（attempt_had_text 只看正文）——思考折起不算发言。
                                reasoning_parts.append(event.delta)
                                yield await emit(ReasoningDeltaEvent(delta=event.delta))
                            elif isinstance(event, llm_base.WebSearchDelta):
                                # 平台工具（云端 web_search）：只转发给前端展示搜索过程，
                                # 不进 blocks/tool_calls、不调注册表、轮末也不写账本（阶段 1）
                                if not attempt_had_text and not turn_notice_emitted:
                                    turn_notice_emitted = True
                                    yield await emit(ToolNoticeEvent())
                                yield await emit(
                                    WebSearchEvent(
                                        phase=event.phase,
                                        search_id=event.search_id,
                                        query=event.query,
                                        sources=event.sources,
                                    )
                                )
                            elif isinstance(event, llm_base.ToolCallReady):
                                blocks.append(event.call)
                                tool_calls.append(event.call)
                            elif isinstance(event, llm_base.TurnEnd):
                                # 记住本轮结束原因：length（输出被截断）要触发升档 / 续写
                                turn_stop_reason = event.stop_reason
                                # 本轮用量单独留一份：total_* 是计费口径的累加，
                                # 而「每轮一行」日志要的是这一轮自己的读数。
                                turn_usage_last = event.usage
                                # 平台搜索次数逐轮累加（按次计费，独立于 token）
                                total_web_searches += event.web_search_count or 0
                                # 用量逐轮累加（每次请求都真实计费，工具轮也不例外），终态报总数
                                if event.usage is not None:
                                    saw_usage = True
                                    total_in += event.usage.input_tokens or 0
                                    total_out += event.usage.output_tokens or 0
                                    # 命中缓存的输入同样逐轮累加（界面用它算"省了多少"）；
                                    # 只在真回了这个字段时才把 saw_cached 置位
                                    if event.usage.cached_tokens is not None:
                                        saw_cached = True
                                        total_cached += event.usage.cached_tokens
                                    # 每轮覆盖：最后留下的是最后一轮发送的全量上下文（环分子）
                                    if event.usage.input_tokens is not None:
                                        self._last_context_input = event.usage.input_tokens
                                    # A2：上轮输出下一轮回灌成输入，同样每轮覆盖交给压缩决策
                                    if event.usage.output_tokens is not None:
                                        self._last_context_output = event.usage.output_tokens

                            # 安全检查点：流事件之间
                            if self._cancel.is_set():
                                stream_cancelled = True
                                break
                finally:
                    # 关闭流、掐断在飞请求。best-effort：此刻终态已定且消息只在轮末才入账，
                    # 关闭异常无可污染的状态，故不向前端抛（R5：不影响结果的清理路径）。
                    with contextlib.suppress(Exception):
                        await agen.aclose()

                if stream_cancelled:
                    # 半截 assistant 不入账，账本停在取消前的完整边界
                    yield await emit(
                        await finish(FinishReason.CANCELLED, iteration, _CANCELLED_TEXT)
                    )
                    return

                if stream_error is not None:
                    if stream_error_context_too_long:
                        # §10 被动应急（路径 2）：真收到上下文超长。与网络重试的区别是
                        # "先减重、再重发"，且应急期间不再调写手。从下一档起取首个可行视图；
                        # 主动应急已用过 strength0 时这里直接从 strength1（下限 1 往返）开始。
                        start_strength = 0 if emergency_strength is None else emergency_strength + 1
                        emergency_view = None
                        if start_strength <= 1:
                            emergency_view, used_strength = self._first_feasible_emergency(
                                effective_window=effective_window,
                                start_strength=start_strength,
                            )
                        if emergency_view is not None and emergency_view.feasible:
                            emergency_messages = list(emergency_view.messages)
                            emergency_strength = used_strength
                            emergency_message = (
                                "这段对话比较长，已自动精简后继续。"
                                if used_strength == 0
                                else "仍然偏长，已只保留最近一轮后继续。"
                            )
                            yield await emit(
                                CompactionEvent(
                                    phase="retrying",
                                    level="emergency",
                                    message=emergency_message,
                                    after_tokens=emergency_view.estimated_tokens,
                                )
                            )
                            # 复用 turn_retry 让 UI 回退半截气泡，再接收减重后的重推
                            yield await emit(
                                TurnRetryEvent(
                                    reason="context_too_long",
                                    attempt=used_strength + 1,
                                    message=emergency_message,
                                )
                            )
                            continue  # 用 emergency_messages 重发当前轮（本轮 assistant 未入账）
                        # 两档都耗尽 / 组不出干净请求（B5）：不再硬撑，明确失败给人话出路
                        give_up = (
                            emergency_view.reason
                            if emergency_view is not None
                            else _EMERGENCY_GIVE_UP_TEXT
                        )
                        yield await emit(
                            CompactionEvent(phase="failed", level="emergency", message=give_up)
                        )
                        yield await emit(ErrorEvent(message=give_up))
                        yield await emit(await finish(FinishReason.FAILED, iteration, give_up))
                        return
                    if stream_error_retryable and stream_attempt < self.max_stream_retries:
                        stream_attempt += 1
                        if stream_retry_reason == "idle_timeout":
                            retry_message = (
                                f"模型已连续 {self.stream_idle_timeout:.0f} 秒没有输出，疑似卡住，"
                                f"正在重试当前轮（第 {stream_attempt} 次）…"
                            )
                        else:
                            retry_message = f"网络中断，正在重试当前轮（第 {stream_attempt} 次）…"
                        yield await emit(
                            TurnRetryEvent(
                                reason=stream_retry_reason,
                                attempt=stream_attempt,
                                message=retry_message,
                            )
                        )
                        # 退避也响应取消：等待期间用户取消就立即收尾，不硬等
                        backoff = self.retry_backoff_base * (2 ** (stream_attempt - 1))
                        async with self._cancel_waiter() as cancel_wait:
                            sleep_task = asyncio.ensure_future(asyncio.sleep(backoff))
                            done, _ = await asyncio.wait(
                                {sleep_task, cancel_wait},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if cancel_wait in done:
                                sleep_task.cancel()
                                await asyncio.gather(sleep_task, return_exceptions=True)
                                yield await emit(
                                    await finish(FinishReason.CANCELLED, iteration, _CANCELLED_TEXT)
                                )
                                return
                        continue  # 重发当前轮（账本本轮未入账，重发幂等）
                    if stream_error_quota:
                        # 主脑额度兜底：仅当本轮"零产出"（没吐字、没声明工具、没联网，
                        # 换脑重发绝对幂等、不会重复上屏 / 重复跑工具 / 重复花联网的钱）且
                        # 还有没用过的兜底脑时，临时换脑重发本轮；复用 turn_retry 让 UI 回退
                        # 半截气泡并显示"换成了谁"。每个兜底脑只试一次（pop 消耗）。
                        produced_anything = (
                            attempt_had_text
                            or bool(blocks)
                            or bool(tool_calls)
                            or turn_notice_emitted
                        )
                        if not produced_anything and fallback_chain:
                            active_model = fallback_chain.pop(0)
                            logger.warning(
                                "主脑 %s 本轮零产出即撞额度/鉴权（%s），临时换 %s 重发",
                                primary_model,
                                stream_error[:120],
                                active_model,
                            )
                            yield await emit(
                                TurnRetryEvent(
                                    reason="model_fallback",
                                    attempt=1,
                                    message=(
                                        f"当前模型 {primary_model or '默认模型'} 暂时没额度"
                                        f"或没权限，已改用 {active_model} 继续回答。"
                                    ),
                                )
                            )
                            continue  # 账本本轮未入账，用兜底脑重发，幂等
                        # 已产出过内容、或兜底脑也用不了：不偷偷重来。中文指路在前、原始
                        # 原因简短附后（不吞平台信息便于排查，也不让长串英文抢主信息）。
                        final_error_message = f"{_QUOTA_GIVE_UP_TEXT}\n（{stream_error[:160]}）"
                    else:
                        final_error_message = stream_error
                    yield await emit(ErrorEvent(message=final_error_message))
                    yield await emit(
                        await finish(
                            FinishReason.FAILED,
                            iteration,
                            f"模型调用失败：{final_error_message}",
                        )
                    )
                    return

                # 流完整成功，但输出被 max_tokens 截断：静默升到更大额度重发当前轮一次。
                # 半截 blocks 直接丢弃（尚未入账），UI 靠 turn_retry 回退后再收重推。
                if turn_stop_reason == "length" and not upgraded:
                    upgraded = True
                    current_max_tokens = self.max_output_tokens_upgrade
                    yield await emit(
                        TurnRetryEvent(
                            reason="max_tokens",
                            attempt=1,
                            message="输出达到长度上限，已放宽上限重新生成本轮…",
                        )
                    )
                    continue

                break  # 本轮流完整消费成功，离开重试循环，进入入账

            # 轮结束：整条 assistant 消息一次性入账（状态原子提交）。
            # 思考先于 assistant 落盘：回放按流水原序画，思考块落在答复 / 工具之前
            if reasoning_parts:
                await self.history.record_reasoning("".join(reasoning_parts))
            assistant = AssistantMessage(blocks=blocks)
            await self.history.record_assistant_message(assistant)
            yield await emit(AssistantMessageEvent(message=assistant))

            # 本轮读数一行（正常路径打点）：放在整条 assistant 入账之后 ——
            # 位置很关键：再往前那段每次流尝试都会经过（截断重发会打两遍），
            # 再往后就只剩「有工具」那条分支（纯文本回复就漏了）。这里每轮恰好一次。
            _log_turn_end(
                iteration,
                turn_stop_reason,
                turn_usage_last,
                len(tool_calls),
                (time.perf_counter() - turn_started) * 1000.0,
            )

            if not tool_calls:
                # 升到最大额度仍被截断：把半截作为第一段入账，续给下一轮接着说，
                # 而不是把没说完的半截当成品返回。
                if (
                    turn_stop_reason == "length"
                    and assistant.text
                    and continuations < self.max_continuations
                ):
                    continuations += 1
                    answer_parts.append(assistant.text)
                    # 内部续写指令只临时进"续写当轮"的 messages() 发给模型：不落盘、不 emit、
                    # 不进历史回放；账本保持 …半截assistant, 续写assistant（无假 user，Bug A）
                    self.history.set_transient_user_message(CONTINUATION_PROMPT)
                    force_text_turn = True
                    logger.info("输出升档后仍截断，续给下一轮（第 %d 段续写）", continuations)
                    continue
                if turn_stop_reason == "length":
                    # 续写额度用尽仍截断：不报错、不无限循环，把已生成的全部内容交给用户
                    logger.warning("已续写 %d 次仍被截断，用已生成内容收尾", continuations)
                final_text = "".join(answer_parts) + assistant.text
                yield await emit(
                    await finish(
                        FinishReason.MAX_ITERATIONS if forcing_summary else FinishReason.FINAL_TEXT,
                        iteration,
                        final_text,
                    )
                )
                return

            if forcing_summary:
                # 摘了工具却仍要求调工具（适配层正常不会让此发生）：防御性按触顶收尾
                yield await emit(
                    await finish(
                        FinishReason.MAX_ITERATIONS,
                        iteration,
                        assistant.text or "已达迭代上限，强制结束。",
                    )
                )
                return

            # 有工具：先统一宣告（started），再并行执行，最后按 id 回灌结果。
            if not attempt_had_text and not turn_notice_emitted:
                turn_notice_emitted = True
                yield await emit(ToolNoticeEvent())
            for call in tool_calls:
                yield await emit(ToolCallEvent(call=call))

            results = await self._execute_tool_batch(tool_calls)

            for call in tool_calls:
                result = results[call.id]
                # 每个 tool_call 恰好闭合一条结果（成功/失败/取消都算），绝不悬空；
                # 结果如何变成发给模型的 ToolMessage 由会话层决定，loop 不关心。
                await self.history.record_tool_result(call.id, result)
                # 工具一条 INFO（正常路径打点）：名字 / 成功失败 / 耗时 / 参数摘要。
                # 摘要是脱敏的关键——字符串参数只报长度，学生作文原文不会落进日志。
                _log_tool_result(
                    result.tool_name,
                    result.ok,
                    result.duration_ms,
                    summarize_args(call.arguments),
                )
                yield await emit(
                    ToolResultEvent(
                        call_id=call.id,
                        name=result.tool_name,
                        ok=result.ok,
                        duration_ms=result.duration_ms,
                        output=result.output,
                        error=result.error,
                    )
                )

            # 安全检查点：工具批次之后（取消也要先把每个 call 闭合完再收尾）
            if self._cancel.is_set():
                yield await emit(await finish(FinishReason.CANCELLED, iteration, _CANCELLED_TEXT))
                return

            # 连续失败计数：这一轮工具全失败则 +1，任一成功则清零；到阈值判 failed
            if all(not results[call.id].ok for call in tool_calls):
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            if consecutive_failures >= self.max_consecutive_tool_failures:
                yield await emit(
                    await finish(
                        FinishReason.FAILED,
                        iteration,
                        f"工具连续 {consecutive_failures} 轮全部失败，已停止。",
                    )
                )
                return

            # 到达迭代上限：下一轮摘掉工具，逼模型总结收尾
            if iteration >= self.max_iterations:
                forcing_summary = True

    # ---------- 上下文压缩（§12.2 / §13）：决策在 history、写手在 loop、落盘回 history ----------

    def _first_feasible_emergency(self, *, effective_window: int, start_strength: int = 0):
        """§10：从 ``start_strength`` 起逐档（0→1）取首个可行应急视图。

        都不可行时返回最后一档（strength=1）视图（``feasible=False``、带人话 reason），
        由调用方发 ErrorEvent 终态。应急不调写手、纯本地渲染，故同步即可。
        """
        view = None
        used = max(0, start_strength)
        for strength in range(max(0, start_strength), 2):
            view = self.history.emergency_view(effective_window=effective_window, strength=strength)
            used = strength
            if view.feasible:
                return view, strength
        return view, used

    def _emit_compaction_metric(self, metric: CompactionMetric) -> None:
        """[D2] 把一次真正发起写手的 L3 尝试交给遥测观察者；旁路、永不抛进主循环。"""
        observer = self.compaction_observer
        if observer is None:
            return
        try:
            observer(metric)
        except Exception:  # noqa: BLE001 - 遥测失败只记录，绝不影响压缩 / 对话
            logger.warning("压缩遥测观察者失败，已忽略", exc_info=True)

    async def _run_compaction(
        self,
        *,
        effective_window: int,
        last_input: int | None,
        last_output: int | None,
        state: dict,
    ) -> AsyncIterator[AgentEvent]:
        """每轮一次的压缩步骤，产出 CompactionEvent；写手期间取消则 yield 取消哨兵。

        三态分发：normal/blocked 直接返回（blocked 的 §10 应急在 413 分支接入）；仅 L2 发
        一条轻提示（plan 内已本地翻转粘滞）；含 L3 则**先 yield started 再调便宜写手**
        （C9：写手慢 / 卡时用户先看到"正在浓缩"），成功 commit 后 done，任何失败 / 无写手
        都 failed/skipped 后让本轮拿全量原文照常发送（§12.4：压缩永不炸主循环）。
        """
        try:
            plan = self.history.plan_compaction(
                effective_window=effective_window,
                last_input_tokens=last_input,
                last_output_tokens=last_output,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 纯本地决策都兜不住时，宁可不压
            logger.warning("压缩决策失败，本轮不压缩、全量继续", exc_info=True)
            yield CompactionEvent(
                phase="skipped",
                level="",
                message=f"压缩决策失败，已跳过：{exc}",
            )
            return

        levels = tuple(plan.levels)
        if plan.state == "blocked":
            # 主动应急（§10 路径 1）：预判已超硬线、本轮大概率发不出。应急不再调写手
            # （到硬线说明主动 L3 已失败 / 不可用，救命只做"tail + 提货单 + B5 原文"），
            # 逐档取首个可行视图，通过 state 交给 inner-while 替换本轮消息重发。
            view, used_strength = self._first_feasible_emergency(
                effective_window=effective_window, start_strength=0
            )
            if view is None or not view.feasible:
                reason = view.reason if view is not None else "上下文过长，无法继续。"
                state["emergency_fatal"] = reason
                yield CompactionEvent(
                    phase="failed",
                    level="emergency",
                    message=reason,
                    before_tokens=plan.predicted_tokens,
                )
                return
            state["emergency_messages"] = list(view.messages)
            state["emergency_strength"] = used_strength
            yield CompactionEvent(
                phase="retrying",
                level="emergency",
                message="这段对话比较长，已自动精简后继续。",
                before_tokens=plan.predicted_tokens,
                after_tokens=view.estimated_tokens,
            )
            return

        if plan.state != "compact" or not levels:
            # normal：无事发生。
            return

        if "l3" not in levels:
            # 仅过 L2 线：plan_compaction 内部已翻转 L2 粘滞（纯本地、无网络），轻提示即可
            yield CompactionEvent(
                phase="l2",
                level="+".join(levels),
                message="较早的批改结果已收起，需要时我会自动调阅。",
            )
            return

        # 过 L3 线：需要便宜写手浓缩旧对话。没配写手（精简部署 / 测试）就不发 started
        # （started 语义是"即将调用写手"）——直接 skipped，保留完整上下文继续。
        if self.compactor is None:
            yield CompactionEvent(
                phase="skipped",
                level="l3",
                message="未配置浓缩模型，本轮保留完整上下文继续。",
                before_tokens=plan.predicted_tokens,
            )
            return

        # 先上屏 started（必须早于写手请求，C9），再调便宜写手浓缩旧对话
        yield CompactionEvent(
            phase="started",
            level="+".join(levels),
            message="对话比较长，正在浓缩较早的内容以便继续…",
            before_tokens=plan.predicted_tokens,
        )

        # 写手请求复用主循环取消等待：用户取消就掐断请求、不落 boundary（§13）
        summarize_started = time.perf_counter()
        summarize_task = asyncio.ensure_future(
            self.compactor.summarize(
                material_records=list(plan.material_records),
                seed_summary_text=plan.seed_summary_text,
                material_truncated=plan.material_truncated,
            )
        )
        async with self._cancel_waiter() as cancel_wait:
            done, _ = await asyncio.wait(
                {summarize_task, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        if cancel_wait in done:
            # 取消优先：即便写手恰好完成也不落 boundary，损失一次便宜调用、状态不花
            if not summarize_task.done():
                summarize_task.cancel()
            await asyncio.gather(summarize_task, return_exceptions=True)
            yield _COMPACTION_CANCELLED
            return

        try:
            result = summarize_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 超长/网络耗尽/空摘要：降级全量继续
            elapsed_ms = (time.perf_counter() - summarize_started) * 1000.0
            logger.warning("L3 浓缩写手失败，本轮不压缩、全量继续：%s", exc)
            self._emit_compaction_metric(
                CompactionMetric(
                    success=False,
                    level="l3",
                    before_tokens=plan.predicted_tokens,
                    after_tokens=None,
                    elapsed_ms=round(elapsed_ms, 1),
                    degraded_reason=exc.__class__.__name__,
                    material_truncated=plan.material_truncated,
                    composition=plan.composition,
                )
            )
            yield CompactionEvent(
                phase="failed",
                level="l3",
                message=f"浓缩失败（{exc}），本轮保留完整上下文继续。",
                before_tokens=plan.predicted_tokens,
                elapsed_ms=round(elapsed_ms, 1),
            )
            return

        elapsed_ms = (time.perf_counter() - summarize_started) * 1000.0

        # 写手内部因材料超长自动对半砍重试成功：补一条 retrying 说明（halve 发生在同一次
        # 等待期内，started 已先上屏，这里在 done 前补可见性即可）
        if getattr(result, "material_halved", False):
            yield CompactionEvent(
                phase="retrying",
                level="l3",
                message="待浓缩内容偏多，已自动精简一半后重新浓缩。",
                material_truncated=True,
            )

        try:
            report = await self.history.commit_compaction(
                plan, result.text, model=getattr(result, "model", None)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - commit 内部本已兜底，这里是双保险
            logger.warning("L3 浓缩结果保存失败，全量继续：%s", exc)
            self._emit_compaction_metric(
                CompactionMetric(
                    success=False,
                    level="l3",
                    before_tokens=plan.predicted_tokens,
                    after_tokens=None,
                    elapsed_ms=round(elapsed_ms, 1),
                    degraded_reason=f"commit:{exc.__class__.__name__}",
                    material_truncated=plan.material_truncated,
                    composition=plan.composition,
                )
            )
            yield CompactionEvent(
                phase="failed",
                level="l3",
                message=f"浓缩结果保存失败：{exc}",
                before_tokens=plan.predicted_tokens,
                elapsed_ms=round(elapsed_ms, 1),
            )
            return

        # 写手耗时只活在 loop 自己的 CompactionEvent / CompactionMetric 里：端口返回的
        # report 由不碰网络、算不出耗时的会话层构造，这里不假设它是可 replace 的 dataclass
        # （§12.4：任何端口形态都不能让压缩步骤炸出主循环）。
        elapsed_value = round(elapsed_ms, 1)

        if report.success:
            self._emit_compaction_metric(
                CompactionMetric(
                    success=True,
                    level=report.level or "l3",
                    before_tokens=report.before_tokens,
                    after_tokens=report.after_tokens,
                    elapsed_ms=elapsed_value,
                    material_truncated=report.material_truncated,
                    boundary_id=report.boundary_id,
                    composition=plan.composition,
                )
            )
            yield CompactionEvent(
                phase="done",
                level=report.level or "l3",
                message="较早的对话已浓缩，原文仍保存在本会话记录里，随时可追问。",
                before_tokens=report.before_tokens,
                after_tokens=report.after_tokens,
                boundary_id=report.boundary_id,
                material_truncated=report.material_truncated,
                elapsed_ms=elapsed_value,
            )
        else:
            self._emit_compaction_metric(
                CompactionMetric(
                    success=False,
                    level=report.level or "l3",
                    before_tokens=report.before_tokens,
                    after_tokens=report.after_tokens,
                    elapsed_ms=elapsed_value,
                    degraded_reason=report.degraded_reason,
                    material_truncated=report.material_truncated,
                    boundary_id=report.boundary_id,
                    composition=plan.composition,
                )
            )
            yield CompactionEvent(
                phase="failed",
                level="l3",
                message=(
                    f"浓缩未能保存：{report.degraded_reason or '未知原因'}，"
                    "本轮保留完整上下文继续。"
                ),
                before_tokens=report.before_tokens,
                elapsed_ms=elapsed_value,
            )

    async def _run_one(self, call: ToolCallBlock) -> tuple[str, ToolResult]:
        """执行单个工具调用并闭合成结果：超时 / 取消 / 异常都有对应结果，绝不抛出。"""
        started = time.perf_counter()
        # 交互式工具（"问用户一句"）不受 tool_timeout 管：它要等人，人多久才点一下
        # 不是程序能猜的；超时豁免后仍受 cancel 约束（用户点停止照样收得住）。
        timeout = None if self.registry.is_interactive(call.name) else self.tool_timeout
        try:
            result = await asyncio.wait_for(
                self.registry.execute(call.name, call.arguments),
                timeout=timeout,
            )
        except TimeoutError:
            result = ToolResult(
                tool_name=call.name,
                ok=False,
                error=(
                    f"工具执行超时：超过 {self.tool_timeout:g} 秒仍未返回，已放弃本次调用。"
                    "请改用别的工具，或把这个任务拆小一点再试。"
                ),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
        except asyncio.CancelledError:
            result = ToolResult(tool_name=call.name, ok=False, error="已被用户取消")
        except Exception as exc:  # noqa: BLE001 - 兜底闭合，绝不让一个 call 悬空
            result = ToolResult(tool_name=call.name, ok=False, error=f"工具执行异常: {exc}")
        return call.id, result

    async def _execute_tool_batch(self, tool_calls: list[ToolCallBlock]) -> dict[str, ToolResult]:
        """执行一轮里的所有工具。三条硬保证，缺一条就会让账本停在半截或 UI 干等：

        1. **每个 call 必有配对结果**：注册表对运行期失败一律返回 ok=False（不抛），
           所以常态是 allSettled —— 一个工具失败不拖累同轮其他工具；
        2. **单个工具有时间上限**：超过 ``tool_timeout`` 就补一条"超时"结果，把决定权
           交回模型（换工具 / 拆小任务 / 直接回答），而不是让整轮永远悬着。
           **例外**：``interactive`` 工具（问用户）不套这个上限，见 :meth:`_run_one`；
        3. **取消收得住**：cancel 后先给工具一段宽限期自行退出，到期仍不退出的
           （同步阻塞、吞掉取消信号的工具）不再等，直接补"已取消"结果收尾。

        编排身份（R10）：全只读批次走并发；批次里只要有一个写入工具，整批
        （含同批只读工具）按模型声明顺序串行执行、两两时间区间严格不重叠。
        """
        if all(self.registry.is_readonly(call.name) for call in tool_calls):
            return await self._execute_parallel(tool_calls)
        return await self._execute_serial(tool_calls)

    @staticmethod
    def _abandon(tasks: list[asyncio.Task]) -> None:
        """把还没结束的批次任务收回来（不等结果），并吞掉取消引发的异常。

        为什么必须有它 —— 取消有两条路，旧实现只覆盖了第一条：
        ① **协作式**：`self._cancel` 被置位，`_cancel_waiter()` 观察到后主动 cancel；
        ② **强行打断**：外层任务被直接 `cancel()`（客户端掉线后 web 层掐掉这次 run），
           此时 `await asyncio.wait(...)` 立刻抛 CancelledError 穿透出去，收尾逻辑
           一行都不执行，在飞的工具会一路跑完。`grade_text` 这类调用最长 120 秒
           且按次计费，白跑一次是实打实的损失（2026-10-01 修）。
        """
        for task in tasks:
            if not task.done():
                task.cancel()
                task.add_done_callback(_swallow_task_exception)

    async def _execute_parallel(self, tool_calls: list[ToolCallBlock]) -> dict[str, ToolResult]:
        """全只读批次：所有工具同时开跑。"""
        tasks = [asyncio.ensure_future(self._run_one(call)) for call in tool_calls]
        gather_task = asyncio.ensure_future(asyncio.gather(*tasks))
        try:
            async with self._cancel_waiter() as cancel_wait:
                done, _ = await asyncio.wait(
                    {gather_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_wait in done and not gather_task.done():
                    # 逐个 cancel：正常协程随即抛 CancelledError，_run_one 把它闭合成一条
                    # "已取消"结果；再给一段宽限期让它们真的收摊
                    for task in tasks:
                        task.cancel()
                    await asyncio.wait({gather_task}, timeout=self.cancel_grace_period)
                finished = gather_task.done()
        except BaseException:
            # 外层被**强行取消**（客户端掉线后上层掐掉这次 run）：协作式取消路径一行都
            # 没走到，在飞的工具会一路跑完（2026-10-01 修，见 _abandon）。
            self._abandon([*tasks, gather_task])
            raise

        if finished:
            return dict(await gather_task)

        # 硬终止路径：宽限期内仍没收工。绝不能因为一个工具不配合就永远挂在这儿 ——
        # 已完成的用真结果，其余一律补"已取消"，保证每个 call 都闭合、账本停在完整边界。
        logger.warning(
            "取消后仍有工具在 %.1f 秒宽限期内未退出，强制放弃并补‘已取消’结果收尾",
            self.cancel_grace_period,
        )
        results: dict[str, ToolResult] = {}
        for task, call in zip(tasks, tool_calls, strict=True):
            if task.done() and not task.cancelled():
                call_id, result = task.result()
                results[call_id] = result
            else:
                results[call.id] = ToolResult(tool_name=call.name, ok=False, error="已被用户取消")
        # 遗孤任务不再等，但要把它掐掉并收走异常，避免 asyncio 在回收时刷警告
        for task in tasks:
            if not task.done():
                task.cancel()
        gather_task.cancel()
        gather_task.add_done_callback(_swallow_task_exception)
        return results

    async def _execute_serial(self, tool_calls: list[ToolCallBlock]) -> dict[str, ToolResult]:
        """含写入批次：按模型声明顺序逐个执行，任一时刻最多一个工具在跑。

        取消 / 超时 / 硬收尾的保证与并发路径完全一致：当前在跑的工具收到取消后
        给宽限期，仍不退就补"已取消"并放弃它；还没轮到的 call 一律补"已取消"。
        """
        results: dict[str, ToolResult] = {}
        orphans: list[asyncio.Task] = []
        started: list[asyncio.Task] = []
        cancelled = False
        hard_stop = False
        try:
            async with self._cancel_waiter() as cancel_wait:
                for call in tool_calls:
                    task = asyncio.ensure_future(self._run_one(call))
                    started.append(task)
                    done, _ = await asyncio.wait(
                        {task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if cancel_wait in done:
                        cancelled = True
                        if not task.done():
                            task.cancel()
                            await asyncio.wait({task}, timeout=self.cancel_grace_period)

                    if task.done() and not task.cancelled():
                        call_id, result = task.result()
                        results[call_id] = result
                    else:
                        # 宽限期到了仍未退出（吞取消信号）：补结果、放弃该任务，不再往后排
                        results[call.id] = ToolResult(
                            tool_name=call.name, ok=False, error="已被用户取消"
                        )
                        task.cancel()
                        orphans.append(task)
                        hard_stop = True
                        break

                    # 取消已发生：当前 call 已闭合，后面排队的不再执行（循环外统一补结果）
                    if cancelled:
                        break
        except BaseException:
            # 同 _execute_parallel：外层强行取消时，当下在飞的那个工具必须收回来
            self._abandon(started)
            raise

        if cancelled or hard_stop:
            if hard_stop:
                logger.warning(
                    "串行批次取消后仍有工具在 %.1f 秒宽限期内未退出，"
                    "强制放弃并为其余调用补‘已取消’结果收尾",
                    self.cancel_grace_period,
                )
            # 还没轮到的 call 全部补"已取消"，保证每个 call 闭合
            for call in tool_calls[len(results) :]:
                results[call.id] = ToolResult(tool_name=call.name, ok=False, error="已被用户取消")

        for task in orphans:
            task.add_done_callback(_swallow_task_exception)
        return results
