"""Web 层共享运行时状态（阶段 1b）。

路由与 runtime 都从这里读写状态；不再 import web.server。
测试可以 monkeypatch `state.<field>`，不会碰真实单例。
"""

from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from glyph.config import Settings
from glyph.context import StudyContext
from glyph.examples import SceneService
from glyph.llm.compactor import CompactionSummarizer
from glyph.llm.responses import ResponsesLLM
from glyph.memory import MemoryCatalogInjector
from glyph.memory.consolidate import MemoryConsolidator
from glyph.memory.extract import MemoryExtractor
from glyph.memory.scheduler import ExtractScheduler
from glyph.plan import PlanStore
from glyph.provider import ProviderConfig
from glyph.runtime.compaction_metrics import CompactionMetric
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.session import SessionHistory
from glyph.study import StudyStore
from glyph.tools import ImageProvider
from glyph.tools.ask_user import AskUserGateway
from glyph.tools.dict_repo import DictRepository, default_dict_path
from glyph.wordbook import WordbookStore


class ImageStore(ImageProvider):
    """内存图片仓：/api/upload 收的图片按 image_id 存字节，供 ocr_image 工具读取。

    实现 tools/ocr.py 的 ImageProvider 协议（工具不认识文件系统，装配层供图）。
    进程内存即可：测试台本地单用户，重启丢图重新上传就是。
    """

    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str]] = {}

    def put(self, data: bytes, mime: str) -> str:
        image_id = uuid.uuid4().hex
        self._items[image_id] = (data, mime)
        return image_id

    async def read(self, image_id: str) -> tuple[bytes, str]:
        item = self._items.get(image_id)
        if item is None:
            raise ValueError(f"图片不存在或已过期：{image_id}")
        return item


@dataclass
class SessionRuntime:
    """一个会话的全部**私有**状态（会话池里的一项）。

    刻意把两类状态分开，这也是「两个会话能同时跑」的全部秘密：

    * 私有（放这里）：流水句柄、主循环实例、会话 id、学习状态摘要、本轮附件、闸、在跑的任务；
    * 共享（留在 RuntimeState）：模型客户端、工具注册表、记忆三件套、词典题库、图片仓……

    私有状态一旦并发就是互相换本子（写错账本不报错、回头查不到），所以必须一会话一份；
    共享状态无会话语义，复制多份只会让 key 重复计费、注册表各建一遍，所以必须共用一份。
    """

    history: SessionHistory
    loop: AgentLoop
    session_id: str
    study_context: StudyContext | None = None
    pending_attachments: list[dict[str, str]] = field(default_factory=list)
    gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 还在用这个会话的请求数（含"已取到会话、还在等闸"的那一段）。
    # 只靠 `loop.busy` 判"在不在用"不够：排队中的请求 busy 是假的，
    # 淘汰 / 删除此时动手会把它正在等的本子关掉。三处（淘汰、删除、改配置）共用这个判据。
    in_flight: int = 0
    # 记忆提炼调度器：**一会话一份**。全局一份的话，A、B 的轮数会累加到同一个
    # "攒够 5 轮"计数上（提前触发），两边同时触发时后到的那次还会被丢掉。
    scheduler: ExtractScheduler | None = None
    extract_task: asyncio.Task[None] | None = None

    def in_use(self) -> bool:
        """这个会话现在有没有人在用（在跑、排队中、或已取到还没开跑）。"""
        return self.in_flight > 0 or self.loop.busy or self.gate.locked()

    def close(self) -> None:
        """关掉流水句柄（收尾落盘）。重复调用安全。"""
        self.history.close()


@dataclass
class RuntimeState:
    """进程内单例状态。

    字段名用不带下划线的短名；路由和 runtime 通过 `state.<field>` 访问。

    会话相关字段（loop / history / session_id / study_context / pending_attachments /
    session_gate / current_run / turn_had_prose）**是「最近活跃会话」的镜像**：
    真正干活时按请求的会话 id 从 `sessions` 池里取，镜像只服务于「不知道会话是谁」
    的老接口（列会话、兼容测试台）。两者由 `runtime._activate` 一处同步，别在别处手改。
    """

    settings: Settings | None = None
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    llm: ResponsesLLM | None = None
    registry: ToolRegistry | None = None
    system_prompt: str = ""
    memory_injector: MemoryCatalogInjector | None = None
    extract_scheduler: ExtractScheduler | None = None
    memory_extractor: MemoryExtractor | None = None
    memory_consolidator: MemoryConsolidator | None = None
    extract_task: asyncio.Task[None] | None = None
    turn_had_prose: bool = True
    compactor: CompactionSummarizer | None = None
    compaction_observer: Callable[[CompactionMetric], None] | None = None
    study_context: StudyContext | None = None
    scene_service: SceneService | None = None
    study_store: StudyStore = field(default_factory=StudyStore)
    pending_attachments: list[dict[str, str]] = field(default_factory=list)
    loop: AgentLoop | None = None
    history: SessionHistory | None = None
    session_id: str | None = None
    current_run: asyncio.Task | None = None
    session_gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 会话池：会话 id → 该会话私有状态。用 OrderedDict 当 LRU（尾部最近用过）。
    # 上限取 settings.session_pool_max；超了淘汰「最久没被用到、且不在跑」的那个。
    # 淘汰只清内存，流水文件不动——下次打开还是原样恢复。
    sessions: OrderedDict[str, SessionRuntime] = field(default_factory=OrderedDict)
    title_cache: dict[str, str] = field(default_factory=dict)
    recheck_ledger: dict[str, dict] = field(default_factory=dict)
    image_store: ImageStore = field(default_factory=ImageStore)
    dict_repo: DictRepository = field(default_factory=lambda: DictRepository(default_dict_path()))
    wordbook_store: WordbookStore = field(default_factory=WordbookStore)
    plan_store: PlanStore = field(default_factory=PlanStore)
    ask_gateway: AskUserGateway = field(default_factory=AskUserGateway)
    upload_dir: Path = field(default_factory=lambda: Path.home() / ".markr" / "uploads")


state = RuntimeState()
