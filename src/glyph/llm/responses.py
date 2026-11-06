"""千问 Responses API（OpenAI 兼容模式）流式适配层。

与 ``qwen.py``（Chat Completions）并列的第二条 LLM 通道：Responses 把"平台内置工具"
（web_search 等，活在千问云端、不由我们后端执行）和"自定义 function"（我们本地执行）
统一在一套 SSE 事件流里：平台 web_search 与本地 function 在同一条流里混用，模型自主
决定联网还是调本地工具，搜索过程与来源对前端可见。

为什么自己解析原始 SSE，而不直接用 SDK 的强类型事件：
- 本机 openai SDK（3.x，底层 httpx2）对 Responses 流式事件的对象模型与官方文档有出入
  （如 web_search_call.in_progress 事件没有 ``.item``）；
- 实测千问流式里三个 ``web_search_call.*`` 事件的 ``item`` 都是空壳，搜索词与来源要到
  ``response.output_item.done`` 的 item.action 才带下来（见 ``_parse_completed_search``）；
- 千问扩展字段（``usage.x_tools.web_search.count`` 搜索计费次数、方言事件
  ``reasoning_text.delta``）可能被 SDK 当 extra 丢弃。
走 ``with_streaming_response`` 拿原始 SSE 文本、自己解析 data JSON，字段与探针所见完全
一致，离线测试只需喂普通 dict。

错误分层与 qwen.py 一致：建连阶段（拿到响应头之前）的可重试错误做有限指数退避重试；
流一旦开始后中断则不就地重试（请求可能已在服务端执行、token 照花），包成可重试的
LLMError 交给主循环决定是否整轮重发。

厂商无关的内部事件仍产出自 ``llm.base``：正文 TextDelta、平台搜索 WebSearchDelta、
轮末 TurnEnd；半截内容同样不吐出。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator

import openai

from ..config import Settings, load_settings
from ..observability import brief
from ..retry import (
    is_context_too_long,
    is_error_body_retryable,
    is_quota_or_auth_error,
    is_retryable,
)
from ..types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    Usage,
    UserMessage,
    WebSource,
)
from .base import (
    ContextTooLongError,
    LLMError,
    ReasoningDelta,
    TextDelta,
    ToolCallReady,
    TurnEnd,
    WebSearchDelta,
)
from .models import supports_reasoning

# ---------- 原始 SSE 文本行 -> data JSON ----------


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    """把 SSE 文本行流归并成 data 负载 JSON 流。

    跳过 id/event 帧、HTTP 状态注释（``:HTTP_STATUS/200``）、空行与坏 JSON；
    遇到 ``[DONE]`` 终止。千问每个事件是单行 ``data: {json}``。
    """
    async for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data:
            continue
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            # 一帧坏数据不应炸掉整条流：跳过它（正常千问不会发坏 JSON）
            continue


logger = logging.getLogger("glyph.llm.responses")

# ---------- 内部消息 -> Responses 请求形状 ----------


def to_responses_input(messages: list[Message]) -> tuple[str | None, list[dict]]:
    """把内部领域消息映射成 Responses 的 (instructions, input items)。

    纯文本多轮：SystemMessage 收敛为顶层 instructions，user / assistant 映射成
    input_text / output_text 内容块。
    阶段 2 新增本地 function 回放：AssistantMessage 的工具块映射成 function_call 项，
    ToolMessage 映射成 function_call_output 项，与平台 web_search 在同一条流里。
    """
    instructions: str | None = None
    items: list[dict] = []

    for message in messages:
        if isinstance(message, SystemMessage):
            instructions = (
                message.content if instructions is None else f"{instructions}\n\n{message.content}"
            )
        elif isinstance(message, UserMessage):
            items.append(
                {"role": "user", "content": [{"type": "input_text", "text": message.content}]}
            )
        elif isinstance(message, AssistantMessage):
            if message.text:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": message.text}],
                    }
                )
            # 已知限制：Responses input 不支持一条 assistant 内文本块与 function_call
            # 交错 —— blocks 为 [text, call, text, call] 时会被拍成"先一条合并文本、
            # 再依次各 call"，丢失交错顺序。真实流里 assistant 多是先说话后调工具，
            # 影响很小；要精确保留得拆成多条 assistant 项，暂不处理。
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    }
                )
        elif isinstance(message, ToolMessage):
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )

    return instructions, items


def to_responses_tools(chat_tools: list[dict] | None, web_search: bool) -> list[dict]:
    """把注册表的 Chat 形 function schema 压扁成 Responses 形，并按需前置 web_search。

    注册表给的是 ``{"type":"function","function":{name,description,parameters}}`` 嵌套，
    Responses 要扁平的 ``{"type":"function","name":...,"description":...,"parameters":...}``。
    平台内置 web_search 与这些自定义 function 在同一个数组里，模型自主挑用。
    """
    tools: list[dict] = [{"type": "web_search"}] if web_search else []
    for entry in chat_tools or []:
        fn = entry.get("function", entry)
        if not isinstance(fn, dict) or "name" not in fn:
            continue
        item: dict = {"type": "function", "name": fn["name"]}
        if fn.get("description"):
            item["description"] = fn["description"]
        if fn.get("parameters"):
            params = fn["parameters"]
            # DashScope 兼容 /responses 校验器要求 object 形参必须带 required 键
            # （空数组也算），全可选参数的工具漏写会整单 400。浅拷贝后补，
            # 不回写注册表共享的 schema（chat 适配层还在用原对象）。
            if (
                isinstance(params, dict)
                and params.get("type") == "object"
                and "properties" in params
                and "required" not in params
            ):
                params = {**params, "required": []}
            item["parameters"] = params
        tools.append(item)
    return tools


# ---------- 厂商事件 dict -> 内部语义事件 ----------


def _parse_completed_search(item: dict) -> WebSearchDelta:
    """从聚合完成的 web_search_call item 取搜索词与来源，产出"搜索完成"事件。

    千问流式里 web_search_call.* 三个事件的 item 是空壳；真正的 action（query /
    queries / sources）要到 response.output_item.done 的 item 上才带得下来，故完成
    事件以 output_item.done 为准（探针实测，勿改回 web_search_call.completed）。
    """
    action = item.get("action", {}) or {}

    query = action.get("query")
    if not query:
        # qwen 会在一次搜索里并行发多个查询词（queries 数组），取第一个作展示
        queries = action.get("queries") or []
        query = queries[0] if queries else None

    sources = [
        WebSource(title=source.get("title"), url=source.get("url"))
        for source in (action.get("sources") or [])
        if isinstance(source, dict) and source.get("url")
    ]
    # 走完流程却没有任何来源 = 没搜到：报 no_result 而非 completed，
    # 前端显失败、模型不得据此称"根据搜索"或编造
    return WebSearchDelta(
        phase="completed" if sources else "no_result",
        search_id=item.get("id"),
        query=query,
        sources=sources,
    )


def _parse_completed_function(item: dict) -> ToolCallReady:
    """从聚合完成的 function_call item 取 call_id / name / arguments，产出可执行调用。

    探针实测：output_item.done 的 function_call item 带完整 arguments（JSON 字符串）
    与 call_id（用于配对 function_call_output）；item.id 是输出项 id，不能当 call_id。
    参数 JSON 解析失败时不发空名调用（与 Chat 通道一致），交上层按截断/失败处理。
    """
    name = item.get("name") or ""
    # 只认 call_id（call_ 前缀）：item.id（msg_）是输出项 id、绝不能拿来配对；
    # 缺 call_id 时用合成 id（让它成为可识别的独立调用），也不冒名顶替 msg_ id。
    call_id = item.get("call_id") or f"call_{name}"
    raw_arguments = item.get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    return ToolCallReady(call=ToolCallBlock(id=call_id, name=name, arguments=arguments))


async def translate_response_events(
    events: AsyncIterator[dict],
) -> AsyncIterator[object]:
    """把 Responses 的 response.* 事件 dict 翻译成内部事件（可注入假 dict 流单测）。

    - ``response.output_text.delta`` → TextDelta（正文逐字）；
    - ``output_item.added`` 且 item 为 web_search_call → 搜索开始（带 search_id 建步骤）；
    - ``web_search_call.in_progress/searching`` → 搜索进行态（空壳，无 id 无词无来源）；
    - ``output_item.done`` 且 item 为 web_search_call → 搜索完成（search_id + 词 + 来源）；
    - ``web_search_call.completed`` 是空壳、``reasoning_text.delta``（千问思考）阶段 1
      不展示不落盘，均忽略；
    - 其余 item 生命周期 / content_part 帧只是骨架，忽略；
    - ``response.failed`` 平台逻辑失败（如配额耗尽）：错误体转成 LLMError 抛出，
      可重试性按错误内容判，绝不当成正常结束（否则前端无限等待、错误不可见）；
    - ``response.completed`` 解析结束状态与用量，流末吐一个 TurnEnd；
    - ``response.incomplete`` 官方截断终态（因 max_output_tokens 等限制提前结束）：
      同样是"平台明确告知的结束"，映射到 length，走主循环的静默升档 / 续写；
    - **流意外结束（EOF）**：SSE 到尾却没有任何终态事件（连接被掐断 / 服务端半途崩），
      抛可重试 LLMError，绝不吐 TurnEnd —— 否则半截回复会以"完整消息"身份入账。
    """
    usage: Usage | None = None
    stop_reason = "stop"
    web_search_count: int | None = None
    function_calls = 0
    # 整轮终态的唯一凭据：只有收到 response.completed 才算"这一轮真的结束了"。
    # 初始 stop_reason="stop" 是兜底值，不是"默认成功"——没有终态就是没成功。
    completed = False

    async for evt in events:
        event_type = evt.get("type")

        if event_type == "response.output_text.delta":
            delta = evt.get("delta")
            if delta:
                yield TextDelta(delta=delta)

        elif event_type == "response.reasoning_text.delta":
            # 思考过程（实测事件名带 response. 前缀）：只展示、不进 blocks、不写账本。
            # 空串帧（有些平台用它表示"思考段落结束"）不上屏，也不打断流。
            reasoning = evt.get("delta")
            if reasoning:
                yield ReasoningDelta(delta=reasoning)

        elif event_type == "response.web_search_call.in_progress":
            yield WebSearchDelta(phase="in_progress")

        elif event_type == "response.web_search_call.searching":
            # 此事件 item 是空壳、拿不到搜索词，先只给"正在搜"的状态，词等完成再带
            yield WebSearchDelta(phase="searching")

        elif event_type == "response.output_item.added":
            # 搜索项刚建立就拿到稳定 search_id：先给前端建 running 步骤占位。
            # 函数调用的 added 此时 arguments 还是空的，不能产出（等 done 拼全），故只处理搜索
            item = evt.get("item", {}) or {}
            if item.get("type") == "web_search_call":
                yield WebSearchDelta(phase="started", search_id=item.get("id"))

        elif event_type == "response.output_item.done":
            # 搜索词 / 来源、函数调用都随聚合完成的 item 在这里才齐
            # （web_search_call.completed 是空壳；函数 arguments 分片此时也已拼全）
            item = evt.get("item", {}) or {}
            if item.get("type") == "web_search_call":
                yield _parse_completed_search(item)
            elif item.get("type") == "function_call":
                function_calls += 1
                yield _parse_completed_function(item)

        elif event_type == "response.failed":
            response = evt.get("response", {}) or {}
            error = response.get("error") or {}
            message = error.get("message") or "模型请求失败（response.failed）"
            code = error.get("code")
            detail = f"{message}（{code}）" if code else message
            # 上下文超长在 SSE 错误体里：detail 已把 code/message 拼在一起，借文字归类
            if is_context_too_long(Exception(detail)):
                raise ContextTooLongError(detail)
            raise LLMError(
                detail,
                retryable=is_error_body_retryable(error),
                quota_or_auth=is_quota_or_auth_error(Exception(detail)),
            )

        elif event_type == "response.incomplete":
            # 官方流式事件：响应因 max_output_tokens 等限制而提前结束 —— 平台**明确
            # 告知**的终态（话没说完，但连接没问题），不是网络故障。
            # 必须映射成 length 让主循环升档 / 续写；若被当成 EOF 去整轮重发，用的还是
            # 同一个额度，只会再撞一次上限、白烧一轮钱。
            # 文档未说明该事件携带 response 体，故缺失时按它的本义（被截断）处理。
            completed = True
            response = evt.get("response") or {"status": "incomplete"}
            usage = _usage_from(response.get("usage") or {})
            web_search_count = _web_search_count(response.get("usage") or {})
            # 截断就是截断：哪怕这一轮已经吐出工具调用，参数也可能只生成了一半，
            # 交主循环升档重发拿完整调用，而不是拿半截入参去执行。
            stop_reason = _stop_reason_from_response(response)

        elif event_type == "response.completed":
            completed = True
            response = evt.get("response", {}) or {}
            raw_usage = response.get("usage") or {}
            usage = _usage_from(raw_usage)
            web_search_count = _web_search_count(raw_usage)
            base_reason = _stop_reason_from_response(response)
            if base_reason == "length":
                stop_reason = "length"
            elif function_calls:
                stop_reason = "tool_calls"
            else:
                stop_reason = "stop"

    if not completed:
        # 一个终态事件都没等到就到尾了：这轮没成功，半截内容必须作废。
        # 可重试（retryable=True）：本轮尚未入账，主循环整轮重发是幂等的，
        # 与"流开始后中断"同一策略；重发预算用尽则由 loop 以 failed 收尾。
        # 不就地重试：请求可能已在服务端执行过、token 照花，交给上层决定。
        raise LLMError("模型响应流意外结束：未收到 response.completed 终态", retryable=True)

    yield TurnEnd(stop_reason=stop_reason, usage=usage, web_search_count=web_search_count)


def _web_search_count(raw_usage: dict) -> int | None:
    """从千问扩展 usage.x_tools.web_search.count 取本轮搜索次数（计费计数）。"""
    x_tools = raw_usage.get("x_tools") or {}
    web_search = x_tools.get("web_search") or {}
    count = web_search.get("count")
    return count if isinstance(count, int) else None


def _usage_from(raw_usage: dict) -> Usage:
    """把平台的 usage 体翻成内部用量（含"命中缓存"）。

    两条通道字段名不同、语义一样，都认：
    - Chat（千问兼容模式）：`prompt_tokens_details.cached_tokens`
    - Responses：`input_tokens_details.cached_tokens`

    平台没给这个字段时保持 None（未知），绝不兜成 0 —— 0 的含义是"一个都没命中"。
    """
    details = raw_usage.get("input_tokens_details") or raw_usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens")
    return Usage(
        input_tokens=raw_usage.get("input_tokens"),
        output_tokens=raw_usage.get("output_tokens"),
        cached_tokens=cached if isinstance(cached, int) else None,
    )


def _stop_reason_from_response(response: dict) -> str:
    """从终态事件的 response.status / incomplete_details 映射结束原因。

    Responses 没有 finish_reason，两种形态都要认：
    - ``response.completed`` 里 status=incomplete（千问也会把截断塞进 completed）；
    - 独立 ``response.incomplete`` 事件（官方：因 max_output_tokens 等限制提前结束）。

    只有"撞上输出上限"才算 length（主循环据此静默升档 / 续写，与 Chat 通道一致）；
    原因明确是别的（如内容过滤）时**不冒充 length** —— 那不是"没说完"，
    再给更大额度重发一次也不会完整，只会白烧一轮。
    """
    status = response.get("status")
    incomplete = response.get("incomplete_details") or {}
    reason = incomplete.get("reason", "") if isinstance(incomplete, dict) else ""
    if reason and "max_output_tokens" not in reason:
        return "stop"  # 平台给了明确原因、但不是输出上限
    if status == "incomplete" or "max_output_tokens" in reason:
        return "length"
    return "stop"


# ---------- 客户端 ----------
# 错误可重试判定不在本文件，统一走 markr/retry.py::is_retryable（全项目单点，R1）


class ResponsesLLM:
    """千问 Responses 通道：平台 web_search 与本地 function 混用的流式联网。

    主循环仍只通过统一的 stream_chat 协议消费它：注册表的本地 function schema 经
    stream_chat 传入、与平台 web_search 一起声明。与 QwenLLM 一样可注入 client 便于测试。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client=None,
        web_search: bool = True,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self._settings = settings or load_settings()
        self.model = self._settings.model
        # 当前模型声明的窗口（0 = 用户没填，压缩层自己找兜底）
        self.model_context_window = int(getattr(self._settings, "model_context_window", 0) or 0)
        self.web_search = web_search
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._client = client or openai.AsyncOpenAI(
            api_key=self._settings.api_key or "missing-api-key",
            base_url=self._settings.base_url,
        )

    def build_payload(
        self,
        messages: list[Message],
        max_tokens: int | None,
        tools: list[dict] | None = None,
        strip_tools: bool = False,
        model: str | None = None,
    ) -> dict:
        """构造 Responses 请求体（纯函数，便于离线断言形状）。

        ``tools`` 与 ``strip_tools`` 是**两个独立维度**，不要互相猜：

        - ``tools=[]`` + ``strip_tools=False``：普通轮，只是这一轮没有本地工具
          （纯搜索会话就长这样）——云端 web_search **照旧声明**，否则搜索根本搜不起来；
        - ``tools=[...]`` + ``strip_tools=False``：混用轮，云端搜索与本地 function
          并排声明，模型自主挑用；
        - ``strip_tools=True``：摘工具轮（主循环的续写轮 / 触顶总结轮）——本地
          function 与云端 web_search **一起摘掉**，逼模型只动嘴；否则续写可能被
          联网答案替代，还白烧一次搜索计费。

        早期这里用 ``bool(tools)`` 一个条件同时承担两件事，于是"纯搜索会话"被误判成
        "摘工具"，demo 与搜索用例的请求里根本没有 web_search（T02）。
        """
        instructions, input_items = to_responses_input(messages)
        # model 覆写用于主脑额度兜底：换脑时连思考档等能力位也按"覆写后的脑"重算，
        # 避免把新脑不支持的参数发出去招来 400；不传则用构造时绑定的 self.model。
        effective_model = model or self.model
        payload: dict = {
            "model": effective_model,
            "input": input_items,
            "stream": True,
            # Responses 的输出上限参数名是 max_output_tokens（Chat 通道叫 max_tokens）
            "max_output_tokens": max_tokens or self._settings.max_tokens,
        }
        if instructions is not None:
            payload["instructions"] = instructions
        if strip_tools:
            payload["tools"] = []
        else:
            # 平台内置 web_search 与本地自定义 function 在同一 tools 数组里混用
            payload["tools"] = to_responses_tools(tools, self.web_search)
        # 思考档（顶层 reasoning.effort）：只对能力表里实测支持该档的大脑下发。
        # 不在表的型号（后端默认 / 旧 .env 型号，未验证思考档）不发，维持模型自身默认，
        # 避免把模型不认的参数发出去招来 400。与是否摘工具无关（续写轮同样可思考）。
        effort = self._settings.reasoning_effort
        # 自定义档按模型目录里声明的档位判（能力表只覆盖内置型号）
        declared = tuple(getattr(self._settings, "reasoning_levels", ()) or ())
        allowed = (effort in declared) if declared else supports_reasoning(effective_model, effort)
        # "关"不发（DSH 的 off 档 wire 值是 null）
        if allowed and effort != "off":
            values = getattr(self._settings, "reasoning_values", {}) or {}
            payload["reasoning"] = {"effort": values.get(effort, effort)}
        return payload

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        strip_tools: bool = False,
        model: str | None = None,
    ) -> AsyncIterator[object]:
        """流式发起一次 Responses 调用并翻译成内部事件。

        tools 为注册表的 Chat 形 function schema，build_payload 压扁后与平台
        web_search 在同一 tools 数组里混用；模型自主决定联网还是调本地函数。
        strip_tools=True 时连云端 web_search 一起摘掉（详见 build_payload）。
        """
        payload = self.build_payload(messages, max_tokens, tools, strip_tools, model=model)
        response, context = await self._open_stream(payload)
        try:
            async for event in translate_response_events(iter_sse_events(response.iter_lines())):
                yield event
        except LLMError:
            raise
        except Exception as exc:
            # 流开始后失败：不就地重试（请求可能已在服务端跑、token 照花），
            # 交给主循环按可重试性决定整轮重发；上下文超长则交 §10 先减重再重发
            if is_context_too_long(exc):
                raise ContextTooLongError(str(exc)) from exc
            raise LLMError(
                str(exc),
                retryable=is_retryable(exc),
                quota_or_auth=is_quota_or_auth_error(exc),
            ) from exc
        finally:
            # 无论正常结束 / 出错 / 被取消，都关闭底层响应，避免连接泄漏
            with contextlib.suppress(Exception):
                await context.__aexit__(None, None, None)

    async def _open_stream(self, payload: dict) -> tuple[object, object]:
        """建连（进入流式响应上下文），对可重试错误做有限指数退避重试。

        返回 (已进入的响应, 异步上下文管理器)；调用方负责在结束时退出上下文。
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            context = self._client.responses.with_streaming_response.create(**payload)
            try:
                response = await context.__aenter__()
                return response, context
            except Exception as exc:  # 建连失败：分类后决定重试或直接失败
                last_exc = exc
                # 上下文超长：重试无意义，直接交 §10 应急
                if is_context_too_long(exc):
                    raise ContextTooLongError(str(exc)) from exc
                if attempt < self.max_retries and is_retryable(exc):
                    # 建连重试一行（与 qwen.py 对齐，设计稿 §2.1-C）：
                    # 走哪条通道都该看得到同样的重试信息。
                    logger.info(
                        "建连失败重试 attempt=%d 退避=%.1fs 原因=%s",
                        attempt + 1,
                        self.backoff_base * (2**attempt),
                        brief(exc),
                    )
                    await asyncio.sleep(self.backoff_base * (2**attempt))
                    continue
                raise LLMError(
                    str(exc),
                    retryable=is_retryable(exc),
                    quota_or_auth=is_quota_or_auth_error(exc),
                ) from exc
        # 理论不可达（循环内要么返回要么抛），兜底以防万一
        raise LLMError(str(last_exc), retryable=True)
