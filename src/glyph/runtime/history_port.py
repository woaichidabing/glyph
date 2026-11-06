"""主循环对「会话历史」的窄端口（依赖倒置）。

``AgentLoop`` 不认识具体的 ``SessionHistory``，更不碰任何文件；它只在这里声明自己需要
的几个方法（端口）。会话层的 ``glyph.session.SessionHistory`` 结构化满足本协议，在装配根
（demo / web / 桌面 sidecar）被构造好再注入进来。

这样三件事同时成立：

- **依赖单向**：runtime 不 import session，session 也不 import runtime，两者都只依赖顶层
  ``glyph.types`` / ``glyph.errors``；"谁接谁"由最外层装配决定，包级依赖图无环；
- **可替换、可测**：测试注入内存假实现（``tests/runtime/fakes.InMemoryHistory``），不写盘；
- **压缩零返工**：将来上上下文压缩，只需换一个实现本端口的 history 对象，``loop`` 一行不改
  —— 这正是接缝 A 方案（循环持有"可整体替换的历史对象"，而不是裸消息数组）的核心收益。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..types import AssistantMessage, FinishReason, Message, ToolResult, Usage

if TYPE_CHECKING:  # runtime 绝不 import session（B2 依赖铁律）；仅类型检查时引用压缩计划/报告
    from ..session.compaction_l3 import CompactionPlan, CompactionReport, EmergencyView


class HistoryPort(Protocol):
    """loop 眼里的会话历史：能取「这一轮发什么」、能记「刚发生了什么」。

    下面这些方法就是全部约定。实现方只需照这里写，就保证能被主循环驱动；
    反过来，主循环**只准**调这里声明过的方法（``tests/runtime/test_loop.py``
    有测试守着这条线）—— 这是 D16「可整体替换」的前提：将来上压缩、换成远端
    实现，``loop`` 一行不改。
    """

    def messages(self) -> list[Message]:
        """返回这一轮该发给模型的消息（一份新列表、只读；system 已在最前、工具已配对）。"""
        ...

    def set_transient_user_message(self, text: str) -> None:
        """临时挂一条**内部**用户消息（如截断续写指令）。

        语义：进了 ``messages()`` **不会被清掉**，一直到任意 ``record_*`` 落账才清 ——
        **只有这一条清除路径**（实现方只需在落账处写一次）。
        它不落盘、不 emit、不进历史回放；传空串等于清除。

        ⚠️ 2026-10-01 修：旧契约是"只进下一次 messages()、用完即弃"（两条清除路径取先到），
        副作用是**续写轮遇上流中断重发时指令已丢** —— 模型不知道要"接着写"，可能把前文
        重写一遍，整轮重发白做。当时改为"看一眼就清"的理由是"怕重发时收到两条一样的
        指令"，但那不会发生：``messages()`` 每次返回**新构造的列表**，临时指令只在末尾
        追加一条，不存在累加。

        loop 用它发内部指令给模型，但账本里不能留下一条用户没说过的「请继续」
        （详见 D18）。和五个 ``record_*`` 的区别一句话：record 是事实，会留在流水里；
        transient 是当轮的临时指令，用完即弃。

        为什么是同步：纯内存赋值、没有 I/O，与 ``messages()`` 同理；只有会落盘的
        ``record_*`` 才是 async。
        """
        ...

    async def record_user_message(self, text: str) -> None:
        """用户说了一句话。"""
        ...

    async def record_assistant_message(self, message: AssistantMessage) -> None:
        """模型一整条输出收完整（文本 + 它带的工具调用）；半截消息绝不调用。"""
        ...

    async def record_reasoning(self, text: str) -> None:
        """模型一轮的思考过程（整轮收完整后整条记一次，半截尝试不调）。

        只落盘给人看（历史会话靠它恢复思考块），**永远不进 messages()** ——
        所以思考再长也不占上下文预算。调用顺序：先 reasoning 后 assistant，
        回放按流水原序画，思考块就落在它解释的那条答复之前。
        """
        ...

    async def record_tool_result(self, call_id: str, result: ToolResult) -> None:
        """一个工具跑完（成功 / 失败 / 取消都算），按 call_id 与前面的调用配对。"""
        ...

    async def record_turn_end(
        self,
        *,
        reason: FinishReason,
        duration_ms: float,
        usage: Usage | None = None,
        context_input_tokens: int | None = None,
        iterations: int,
    ) -> None:
        """一次 run 收尾：为什么停 / 耗时 / 用量 / 轮数。只进流水，不进模型上下文。"""
        ...

    async def record_cancel(self, *, reason: str = "user", iteration: int | None = None) -> None:
        """一次取消。只进流水，不进模型上下文。"""
        ...

    # ---------- 上下文压缩（设计稿 §12.2 · B2：history 出决策/素材与落盘，不碰网络） ----------

    def plan_compaction(
        self,
        *,
        effective_window: int,
        last_input_tokens: int | None,
        last_output_tokens: int | None,
        estimated_tokens: int | None = None,
        new_user_estimated_tokens: int = 0,
    ) -> CompactionPlan:
        """本轮发送前的**纯本地**压缩决策（不碰网络、不改流水）。

        三态：normal（不压）/ compact（过 L2 或 L3 线，``levels`` 标明级别）/
        blocked（预判已超硬线，本轮大概率发不出，loop 转 §10 应急）。过 L2 线时本方法
        内部顺带翻转 L2 粘滞（纯本地）；过 L3 线时返回的 plan 已切好动态 tail、装完写手
        材料与切点，loop 拿到后自己调便宜写手、再回调 :meth:`commit_compaction`。

        ``estimated_tokens`` 为 None 时由实现方自行 measure 当前消息兜底换算（loop 在
        runtime 层、拿不到 session 的称重函数）；有真实 usage（``last_input_tokens`` 非
        None）时决策以真实水位为准。一轮最多调一次（网络重发不重跑，见 §13）。
        """
        ...

    async def commit_compaction(
        self,
        plan: CompactionPlan,
        summary_text: str,
        *,
        model: str | None = None,
    ) -> CompactionReport:
        """写手成功后落账：先追加摘要记录、再追加 boundary 记录（原子提交点，§13）。

        返回压前 / 压后水位与 boundary_id；任何非取消异常都降级为 ``success=False``
        报告、绝不炸主循环（§12.4），``CancelledError`` 单独向上抛（取消不落 boundary）。
        """
        ...

    def emergency_view(self, *, effective_window: int, strength: int = 0) -> EmergencyView:
        """§10 413 应急的**临时减重渲染**（纯本地、不调写手、不落盘、不改流水）。

        主动 blocked（轮开头预判超硬线）与被动 413（stream_chat 真收到上下文超长）汇合
        到同一动作时由 loop 调用：返回只含"最近 N 往返 + 已有摘要 / 全量提货单 + B5 强制
        保留的未结束任务 user 原文"的可重发消息。``strength`` 0=首次、1=再缩到下限 1 往返；
        连下限都组不出干净请求时 ``feasible=False``、``reason`` 是给用户的人话出路。
        应急不受 ``GLYPH_AUTO_COMPACT`` 总开关影响（C3：保命逻辑始终生效）。
        """
        ...
