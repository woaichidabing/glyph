"""read_artifact：从当前会话全量流水**只读**取回已装箱的批改成品 / 定稿（§8.4，#64）。

为什么需要它：L2 装箱把旧批改全文从上下文撤下、只留提货单（§8.3），但原文一字不删地
留在会话流水里。模型要讲旧细节时自己调本工具按编号取回，用户无感（§8.4）。

安全边界：只读（继承 ReadOnlyTool，可并发）；**没有路径参数**，读取器在装配时绑定
"当前会话"，天然不能出会话、不能任读磁盘。工具不直接持有会话，而经 provider 回调在
运行期取当前会话读取器——registry 跨会话复用，切会话后自动指向新会话（与
build_attempts 的 session_id_provider 同模式）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..runtime.errors import ToolError
from .base import ReadOnlyTool

__all__ = ["ReadArtifactTool", "ArtifactReader"]


class ArtifactReader(Protocol):
    """会话侧只读成品读取器协议（SessionHistory 隐式实现，tools 不反向依赖 session）。"""

    def artifact_catalog(self) -> dict[str, Any]: ...

    def read_artifact(
        self,
        call_id: str,
        *,
        item_id: str | None = None,
        which: str = "result",
    ) -> dict[str, Any] | None: ...


class ReadArtifactTool(ReadOnlyTool):
    name = "read_artifact"
    description = (
        "调阅本会话里较早的批改成品或用户定稿。上下文压缩后，更早的作文批改全文会收成"
        "一张“提货单”，但原文仍完整保留在本会话记录里，用本工具按编号取回，然后照常回答细节。\n"
        "用法：\n"
        "- 不传 call_id：返回保险柜里全部成品的一行目录（想不起编号、或提货单提示有省略时，"
        "先查目录）；\n"
        '- call_id + which="result"（默认）：取回那篇的原始批改（原文 + 逐条问题）；\n'
        "- 再给 item_id：只取回某一条问题；\n"
        '- which="final"：取回那篇用户确认后的定稿全文。\n'
        "只读、只能读本会话、不接受文件路径。找不到会明确报错——按提示换编号或先查目录，不要凭印象编造。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": "提货单 / 目录里的成品代号；不传则返回全量一行目录",
                "minLength": 1,
            },
            "item_id": {
                "type": "string",
                "description": "只要某一条问题时填它的 id；不传返回整份成品",
                "minLength": 1,
            },
            "which": {
                "type": "string",
                "enum": ["result", "final"],
                "description": "result=原始批改成品（默认）；final=用户定稿版",
                "default": "result",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, *, history_provider: Callable[[], ArtifactReader | None]) -> None:
        self._history_provider = history_provider

    async def run(self, args: dict[str, Any]) -> dict[str, Any]:
        reader = self._history_provider()
        if reader is None:
            raise ToolError("当前没有活动会话，暂时无法调阅旧批改。")

        call_id = args.get("call_id")
        item_id = args.get("item_id")
        which = args.get("which") or "result"
        if which not in ("result", "final"):
            raise ToolError(f"which 只能是 result 或 final，收到：{which!r}")

        # 不传 call_id：先给全量目录（配合提货单省略，模型永远能问“保险柜里还有什么”）
        if not call_id:
            return reader.artifact_catalog()

        data = reader.read_artifact(call_id, item_id=item_id, which=which)
        if data is not None:
            return data

        # 找不到：按入参给可操作的友好错误，引导模型换编号 / 查目录
        if which == "final":
            raise ToolError(
                f'成品 {call_id} 还没有定稿版；可改用 which="result" 取回它的原始批改。'
            )
        if item_id is not None:
            raise ToolError(
                f"成品 {call_id} 里没有编号为 {item_id} 的条目；可不带 item_id "
                "取回整份后核对条目编号。"
            )
        raise ToolError(
            f"没找到编号为 {call_id} 的成品，编号可能有误；可先不带 call_id 调 "
            "read_artifact 查看全量目录。"
        )
