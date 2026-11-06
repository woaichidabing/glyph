"""事件 sink：loop 不关心事件最终送到哪（UI / 内存收集 / 以后落盘）。

第一层只提供协议与 CollectingSink；持久化的 JsonlFileSink 等做到对应阶段再加，
loop 本体无需改动（事件流是唯一事实源）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..events import AgentEvent


@runtime_checkable
class EventSink(Protocol):
    async def on_event(self, event: AgentEvent) -> None: ...


class CollectingSink:
    """把事件收集到内存列表，主要用于测试断言。"""

    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)
