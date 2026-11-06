"""Web 层运行时：状态读写、装配、生命周期与跨路由公共函数（阶段 1b）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

# 这些 import 也给路由用（路由经 runtime as srv 取公共常量 / 类型）。
# ruff: noqa: F401
from .deps import SessionRuntime, state

# 不做 editable 安装（R9），把 src 加进导入路径
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

import openai  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from glyph.config import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SESSION_POOL_MAX,
    REASONING_LEVELS,
    REASONING_LEVELS_ALL,
    Settings,
    load_settings,
    normalize_context_mode,
    normalize_speaking_style,
)
from glyph.context import (  # noqa: E402
    StudyContext,
    attachments_fragment,
    env_date_fragment,
    study_fragment,
)
from glyph.llm.compactor import CompactionSummarizer  # noqa: E402
from glyph.llm.models import (  # noqa: E402
    DEFAULT_MAIN_MODEL_ID,
    MAIN_MODELS,
    MainModel,
    is_supported_main_model,
    network_capable,
    reasoning_levels_for,
    supports_reasoning,
)
from glyph.llm.qwen import QwenLLM  # noqa: E402
from glyph.llm.responses import ResponsesLLM  # noqa: E402
from glyph.memory import MemoryCatalogInjector  # noqa: E402
from glyph.memory.consolidate import MemoryConsolidator  # noqa: E402
from glyph.memory.extract import MemoryExtractor  # noqa: E402
from glyph.memory.paths import memory_root  # noqa: E402
from glyph.memory.scheduler import ExtractDecision, ExtractScheduler, TurnSample  # noqa: E402
from glyph.memory.store import MemoryStore  # noqa: E402
from glyph.observability import configure_logging  # noqa: E402

# 日志器名必须挂在 markr 树下：observability.configure_logging 只配置 "glyph" 父日志器
# （propagate=False），用 __name__（web.server）会既不进文件也不上控制台。
logger = logging.getLogger("glyph.web")
from glyph.provider import (  # noqa: E402
    SOURCE_CUSTOM,
    ProviderConfig,
    ProviderDiscoverError,
    apply_legacy,
    apply_provider,
    capabilities_for,
    capabilities_payload,
    fetch_upstream_models,
    load_provider_config,
    merge_provider,
    repair_role_models,
    save_provider_config,
)
from glyph.provider.book import (  # noqa: E402
    KIND_BUILTIN,
    KIND_CUSTOM,
    ROLE_IDS,
    ModelEntry,
    ProviderBook,
    ProviderEntry,
    active_provider_id,
    book_from_dict,
    book_to_dict,
    bumped,
    default_credential_ref,
    entry_from_dict,
    entry_to_dict,
    first_valid,
    hydrate_credentials,
    model_can_see_image,
    normalize_models,
    settle_active,
    to_legacy,
    validate_entry,
)
from glyph.provider.store import load_provider_book, save_provider_book  # noqa: E402
from glyph.retry import is_context_too_long, is_quota_or_auth_error  # noqa: E402
from glyph.runtime.compaction_metrics import (  # noqa: E402
    CompactionMetric,
    file_compaction_observer,
)
from glyph.runtime.errors import ToolError  # noqa: E402
from glyph.runtime.loop import AgentLoop  # noqa: E402
from glyph.runtime.registry import ToolRegistry  # noqa: E402
from glyph.runtime.tool import Tool  # noqa: E402
from glyph.session import (  # noqa: E402
    SessionCorruptError,
    SessionHistory,
    SessionNotFoundError,
    SessionWriteError,
    new_session,
    open_session,
)
from glyph.session.compaction import (  # noqa: E402
    composition_tokens,
    estimate_tokens_from_bytes,
)
from glyph.session.paths import (  # noqa: E402
    is_valid_session_id,
    local_now,
    session_path,
    sessions_root,
)
from glyph.session.records import (  # noqa: E402
    FinalizePayload,
    RecordType,
    build_record,
)
from glyph.session.stats import summarize_sessions  # noqa: E402
from glyph.session.store import SessionStore, iter_session_summaries  # noqa: E402
from glyph.tools import ImageProvider  # noqa: E402
from glyph.tools.ask_user import AskUserGateway  # noqa: E402
from glyph.tools.assemble import build_default_registry  # noqa: E402
from glyph.tools.base import ReadOnlyTool  # noqa: E402
from glyph.tools.dict_repo import TAG_ORDER, DictRepository, default_dict_path  # noqa: E402
from glyph.tools.grading_models import Detection  # noqa: E402
from glyph.tools.review import (  # noqa: E402
    apply_decisions,
    build_marked_segments,
    build_result_text,
    finalize_text,
)
from glyph.wordbook import WordbookStore  # noqa: E402

# （calculator / word_count 两个联调假工具已从本台移除：它们不是产品能力；
# 需要 function calling 的最小可运行示例见 examples/chat_demo.py。）


# ---------- 会话装配：llm / registry 复用，loop 随会话切换 ----------

# 「模型问用户一句」的回答通道：**全进程唯一一条**，在这里 new 一次、随注册表一起
# 复用（不随会话切换、也不随设置页重建——它不持有任何钥匙或路径）。
# 为什么必须是模块级单例：/api/ask-user/answer 要把用户的答复送进**那个**挂起中的
# ask_user 工具；换会话 / 换钥匙后如果通道跟着换，界面上点选项就会石沉大海。


# 复核账本（§6.4）：外层 key = session_id，内层 = {"per_item": {id: 次数}, "total": 次数}。
# 只存在于 web 装配层——不进 tools/（工具无状态）、不进 session/（JSONL 是只追加流水）、
# 不放前端内存（刷新页面就丢，配额会和工具侧兜底对不上）。

# 图片仓单例：跨会话复用（图片 id 全局唯一即可，与账本不同不需要按会话分）

# 本地 ECDICT 查词仓单例：只读、不依赖钥匙 / 会话，导入即可用；db 缺失时安全降级

# 用户生词本 / 查词历史单例：单文件 JSON，根目录可用 GLYPH_WORDBOOK_ROOT 覆盖；
# 测试里 monkeypatch 成 tmp_path 下的 store（不碰真实 ~/.markr）。


class _QuotaGuardedRecheckTool(ReadOnlyTool):
    """给复核工具套一层"每条 / 每次会话最多复核几次"的配额闸。

    注意：本闸只包**非交互式**工具（当前只有 recheck_detection）。若将来要包交互式
    工具，必须把内层身份透传过来（`interactive`），否则注册表读到的是基类的 False，
    "问用户一句"会被 2 分钟超时掐断 —— 而界面上一句话都不会说。
    """

    """recheck_detection 的配额闸（MSG-010 ③：配额从前端搬到服务端）。

    - 单条上限 = settings.recheck_max_rounds（与工具的 round 兜底同源）；
    - 单篇上限 = settings.recheck_max_total（**web 独占负责**，工具不认识"篇"）。
    - 超限抛 ToolError：错误即数据回灌模型，主模型翻译成人话，循环不死。

    账本按**当前会话**记（run 时动态读 _session_id）：registry 跨会话复用，
    切会话后同一个 wrapper 自动查新会话的账本。
    """

    def __init__(self, inner: Tool, *, max_per_item: int, max_total: int) -> None:
        self._inner = inner
        self._max_per_item = max_per_item
        self._max_total = max_total
        # 透传真工具的对外契约：模型看到的名字与 schema 不变
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters

    async def run(self, args: dict) -> dict:
        # 账本按**本轮**会话记：两个会话并发复核时不能共用一条计数
        sid = current_session_id() or "unknown"
        used = state.recheck_ledger.setdefault(sid, {"per_item": {}, "total": 0})
        det_id = str((args.get("detection") or {}).get("id") or "")
        if used["total"] >= self._max_total:
            raise ToolError(
                f"本篇复核总次数已用完（{self._max_total} 次）。"
                "你可以维持自己的判断：采纳、改成自己的版本，或直接剔除此条。"
            )
        if used["per_item"].get(det_id, 0) >= self._max_per_item:
            raise ToolError(
                f"这条已复核 {self._max_per_item} 次，次数已用完。"
                "你可以维持自己的判断：采纳、改成自己的版本，或直接剔除此条。"
            )
        result = await self._inner.run(args)
        # 真工具失败（抛 ToolError）不会走到这里：没复核成就不算次数
        used["per_item"][det_id] = used["per_item"].get(det_id, 0) + 1
        used["total"] += 1
        return result


def llm_channel_for(settings: Settings) -> str:
    """按当前档的协议决定用哪条通道（DSH 的 provider.api 对应物）。

    - openai-chat → Chat Completions 通道（qwen.QwenLLM）
    - 其余（含缺省）→ Responses 通道（responses.ResponsesLLM）
    - anthropic → 尚未实现，保存时就会被挡下，真到了这里按 Responses 兜底
    """
    protocol = (getattr(settings, "api_protocol", "") or "").strip().lower()
    return "chat" if protocol == "openai-chat" else "responses"


def _build_runtime(
    settings: Settings,
) -> tuple[
    ResponsesLLM,
    ToolRegistry,
    str,
    MemoryCatalogInjector,
    CompactionSummarizer | None,
    Callable[[CompactionMetric], None],
    ExtractScheduler,
    MemoryExtractor,
    MemoryConsolidator,
]:
    """模型客户端 + 工具注册表：跨会话复用，不随会话切换重建。

    settings 由调用方传入：设置页换钥匙后，用新 Settings 重跑本函数即可整体重建
    （grade / ocr / describe 等模型工具内部也持有 key，必须连同 registry 一起重建）。
    """
    if not settings.api_key:
        print("[markr-web] 警告：未配置 DASHSCOPE_API_KEY，模型调用会失败。")
    # 没配 Key 时也要能起来：新用户第一次打开时还没有任何 Key，
    # 这里用占位值满足客户端构造，真正调用模型时再报"未配置"（界面另有引导）。
    client = openai.AsyncOpenAI(
        api_key=settings.api_key or "sk-not-configured",
        base_url=settings.base_url,
        timeout=120.0,
    )
    # 联网 = 全局开关 ∩ 所选型号自身能力：flash / 27b 等不挂平台联网的型号，开关再开也不下发
    if llm_channel_for(settings) == "chat":
        # 第三方 OpenAI Chat 兼容端点：走老的 Chat 通道（不发平台 web_search）
        llm = QwenLLM(settings, client=client)
    else:
        llm = ResponsesLLM(
            settings,
            client=client,
            web_search=network_capable(settings.model, settings.web_search),
        )
    # L3 上下文浓缩写手（便宜长窗口模型，默认 qwen3.7-flash；与主脑共用同一把聚合 key /
    # base_url）。auto_compact 关闭时不构造：主动 L2/L3 停用（413 应急不受影响，见 C3）。
    compactor = CompactionSummarizer(settings, client=client) if settings.auto_compact else None
    # D2 压缩遥测：每次 L3 尝试追加一行到 ~/.markr/compaction.jsonl（旁路、写失败不炸）。
    compaction_observer = file_compaction_observer(Path.home() / ".markr" / "compaction.jsonl")
    # 工具统一装配：全系统内置工具在 tools/assemble.py 一处登记（唯一台账）。
    # web 层专属状态经参数注入——图片 / 词典仓、上传目录白名单、当前会话 id 与流水回调；
    # 复核配额闸（账本是 web 层状态，不进 tools/）经 recheck_wrapper 套上后再注册。
    registry, mem_injector, system_prompt = build_default_registry(
        settings,
        image_store=state.image_store,
        dict_repo=state.dict_repo,
        upload_dirs=[state.upload_dir],
        session_id_provider=current_session_id,
        history_provider=current_history,
        # 必须显式传同一个通道：端点靠它把用户的答复送进挂起中的 ask_user 工具。
        # 不传的话装配层会自己 new 一个 —— 用户点了选项而工具永远等不到（静默失效）。
        ask_gateway=state.ask_gateway,
        # 收词卡要读“查过几次 / 是否已收”，必须和 /api/user/wordbook 用同一个 store；
        # 不传的话装配层拿到 None，卡片只会显示 0 次（不会写坏数据，但会误导）。
        wordbook_store=state.wordbook_store,
        plan_store=state.plan_store,
        study_store=state.study_store,
        recheck_wrapper=lambda inner: _QuotaGuardedRecheckTool(
            inner,
            max_per_item=settings.recheck_max_rounds,
            max_total=settings.recheck_max_total,
        ),
    )
    # 长期记忆 v2（D27/D29）：提炼与整理都复用压缩写手那个便宜模型（不新增模型 / client）。
    # 三者都跨会话复用：调度器有轮数与游标状态，提炼器与整理器无状态。
    scheduler = ExtractScheduler(
        every_turns=settings.memory_extract_every_turns,
        idle_seconds=settings.memory_extract_idle_seconds,
    )
    # 压缩写手就是提炼/整理的模型通道（不新增模型）；非自动压缩时现造一个同款
    extract_client = (
        compactor if compactor is not None else CompactionSummarizer(settings, client=client)
    )
    extractor = MemoryExtractor(
        root=memory_root(),
        llm=extract_client,
        model=settings.compact_model,
        max_rounds=settings.memory_extract_max_rounds,
    )
    consolidator = MemoryConsolidator(
        root=memory_root(),
        store=MemoryStore(root=memory_root()),
        llm=extract_client,
        model=settings.compact_model,
        min_hours=settings.memory_consolidate_min_hours,
        min_sessions=settings.memory_consolidate_min_sessions,
        throttle_seconds=settings.memory_consolidate_throttle_seconds,
    )
    return (
        llm,
        registry,
        system_prompt,
        mem_injector,
        compactor,
        compaction_observer,
        scheduler,
        extractor,
        consolidator,
    )


def _make_loop(
    settings: Settings,
    llm: ResponsesLLM,
    registry: ToolRegistry,
    history: SessionHistory,
    compactor: CompactionSummarizer | None = None,
    compaction_observer: Callable[[CompactionMetric], None] | None = None,
) -> AgentLoop:
    """给定一个 history，装配出 AgentLoop（额度参数由传入 settings 注入）。"""
    return AgentLoop(
        llm,
        registry,
        history=history,
        context_mode=settings.context_mode,
        main_fallback_models=settings.main_fallback_models,
        max_output_tokens=settings.max_tokens,
        max_output_tokens_upgrade=settings.max_tokens_upgrade,
        stream_idle_timeout=settings.stream_idle_timeout,
        tool_timeout=settings.tool_timeout,
        compactor=compactor,
        compaction_observer=compaction_observer,
    )


def _apply_rebuild(new_settings: Settings, new_provider: ProviderConfig | None = None) -> None:
    """整体重建运行时，并把池里每个会话的循环重挂一遍（换配置的唯一落地路径）。

    设置页保存（POST /api/config）与供应商多档切换（/api/providers 三个写接口）都走这里：
    重建共享的模型客户端 / 注册表 / 系统词 / 压缩写手，再逐会话重挂循环——每个会话的循环
    持有旧 settings 的额度参数，不换就是新旧配置混着跑。给了 new_provider 就顺手把
    "当前生效的供应商配置"镜像换掉（它和 settings 必须同源，否则界面回显会与实际不符）。
    """
    # 设置页可能改了日志级别 / 路径：重新接线一次（configure_logging 幂等，不会叠加
    # handler），否则用户在界面上改完要重启进程才生效。
    configure_logging(level=new_settings.log_level, log_file=new_settings.log_file)
    (
        state.llm,
        state.registry,
        state.system_prompt,
        state.memory_injector,
        state.compactor,
        state.compaction_observer,
        state.extract_scheduler,
        state.memory_extractor,
        state.memory_consolidator,
    ) = _build_runtime(new_settings)
    state.settings = new_settings
    if state.sessions:
        for session in state.sessions.values():
            session.loop = _make_loop(
                new_settings,
                state.llm,
                state.registry,
                session.history,
                compactor=state.compactor,
                compaction_observer=state.compaction_observer,
            )
        # 重建完把镜像指回**原来那个**会话，不要顺手跳到"最近用过的那个"：
        # 界面还停在原会话上，镜像换了会让列会话的 is_current 与定稿的兜底路径对不上。
        current = state.sessions.get(state.session_id or "")
        _sync_mirror(current or _latest_session())
    elif state.history is not None:
        state.loop = _make_loop(
            new_settings,
            state.llm,
            state.registry,
            state.history,
            compactor=state.compactor,
            compaction_observer=state.compaction_observer,
        )
    # 例句服务缓存了 Settings（key / 模型）；换配置后必须重建，
    # 否则先没 key 再补 key 会一直生成失败。
    state.scene_service = None
    if new_provider is not None:
        state.provider = new_provider


def credential_store():
    """凭据库（与 DSH 的 credentials seam 对应）：配置里只放名字，值在这里。"""
    from glyph.credentials import CredentialStore

    return CredentialStore()


def provider_book() -> ProviderBook:
    """磁盘上的整本供应商配置（v2 多档）。**每次现读**，进程内不留副本。

    为什么不缓存：磁盘是唯一事实源。缓存一份就得在启动 / 保存 / 测试三处同步，
    漏一处就会出现"界面上切了档、后端还按旧档跑"这种最难查的错。文件很小，读一次可忽略。
    v1 老文件在这里被 store 读时迁移成 v2 形状（读路径无副作用、不写盘）。
    """
    book = load_provider_book()
    # 凭据水合：老数据里的明文钥匙迁到凭据库（之后配置里只剩 ref）；
    # 已有 ref 的档把值解析回来供发请求用。幂等，读路径可以反复调。
    store = credential_store()
    hydrated = hydrate_credentials(book, store)
    # 关键顺序：配置文件里没有明文钥匙，所以**先补水再落定当前档**；
    # 否则刚加的那一档会被判成"没配好"，active 掉回内置千问（2026-10-28 真踩到）。
    hydrated = settle_active(hydrated)
    # 迁移后若有"只存在明文、没有 ref"的档，回写一次（明文随即从配置文件消失）
    if any(not raw.api_key_env and raw.api_key for raw in book.providers):
        try:
            save_provider_book(_without_plaintext(hydrated))
        except OSError:
            pass
    return hydrated


def _without_plaintext(book: ProviderBook) -> ProviderBook:
    """落盘形状：**永不写明文钥匙**（只留 api_key_env）。"""
    return replace(book, providers=tuple(replace(e, api_key="") for e in book.providers))


def persist_book(book: ProviderBook) -> ProviderBook:
    """唯一落盘口：先把明文钥匙迁进凭据库，再写文件（序列化会剥掉明文）。"""
    hydrated = hydrate_credentials(book, credential_store())
    save_provider_book(hydrated)
    return hydrated


def save_and_apply_book(book: ProviderBook) -> ProviderBook:
    """把整本配置落盘并让它立刻生效（增删档 / 切档保存后的唯一出口）。

    两件事按顺序做，顺序不能反：
    1. 先归一"当前启用"（指向已删 / 没配好的档时落到第一个可用档），再原子写盘；
    2. 再把归一后的当前档换算成运行时认识的配置（to_legacy），走既有整体重建路径。

    返回归一后的整本配置——调用方直接拿它回给前端，前端不必再猜"我删了 active 之后谁上位"。
    """
    # 每次保存 +1 版：前端拿旧版号来保存时就能发现"期间被别处改过"。
    # 注意**不要**用 book_from_dict(book_to_dict(...)) 做归一：序列化会剥掉明文钥匙，
    # 归一回来那一档就"没配好"了（2026-10-28 真踩到：新加的档启用不了）。
    settled = persist_book(bumped(book))
    # 落盘后把凭据补回内存（运行时要用；文件里仍只有引用名），再重新落定当前档
    settled = settle_active(hydrate_credentials(settled, credential_store()))
    rebuild_after_provider_change(to_legacy(settled))
    return settled


def rebuild_after_provider_change(provider: ProviderConfig) -> Settings:
    """按新的供应商配置重建运行时（保留用户已选的思考档 / 上下文档 / 说话风格）。

    与 POST /api/config 的差别只在入口：那边收到的是一个补丁请求（可只改主脑 / 思考档），
    这里收到的是一份算好的配置。落地点完全一样（apply_provider + _apply_rebuild），
    不另起一条装配路径——两条装配路径早晚会漂移。
    """
    settings = apply_provider(load_settings(), provider)
    # 思考档：按"这一档上次选的"恢复（重启后不丢）；没记过才用 .env 的默认
    book_efforts = provider_book().last_efforts
    remembered = book_efforts.get(active_provider_id(provider_book()), "")
    if remembered:
        settings = replace(settings, reasoning_effort=remembered)
    current = state.settings
    if current is not None:
        settings = replace(
            settings,
            reasoning_effort=current.reasoning_effort,
            context_mode=current.context_mode,
            speaking_style=current.speaking_style,
        )
    _apply_rebuild(settings, provider)
    return settings


# 当前生效配置：启动时来自 .env；设置页保存钥匙后整体替换，是装配层唯一配置源
# 当前模型来源（默认千问 / 自定义 OpenAI 兼容）：启动时从 ~/.markr/provider.json 恢复，
# 设置页保存时落盘；_settings 是它叠加到 .env 之后的结果。
# 跨会话复用的运行时（lifespan 里装配一次）
# 按注册表实际工具清单动态组装的系统提示词（lifespan 里重建一次）
# —— 模块级 SYSTEM_PROMPT 只含核心工具，这里注册了额外工具，必须重建否则对模型是死的
# 长期记忆目录注入器：作为上下文片段回调每轮合成，不落盘、不随会话切换
# 长期记忆 v2（D27/D29）：提炼调度器 + 提炼器 + 整理器。
# 三个都跨会话复用；调度器每次轮结束 observe 一次，决定要不要跑后台提炼。
# 提炼任务句柄与「这一轮用户有没有打过字」的标记。
# 前者防重复开跑（D27：上次没跑完就不开新的）；后者是三条前置过滤的第一条。
# L3 上下文浓缩写手 + D2 遥测观察者（lifespan / 设置页重建时随运行时一起装配）
# T06 只读学习状态摘要：每次 /api/ask 带上就刷新，由上下文回调每轮取最新值。
# 后端不独立读浏览器本地存储，只做转发与渲染 —— 这是 A 方案（前端带摘要）的边界。
# 本轮用户上传的图片（文件名 + image_id）：/api/ask 每次刷新，上下文回调每轮取。
# 只陈述事实，不拼进用户消息——用户原话一个字都不动。


def _parse_study_context(raw: str | None) -> StudyContext | None:
    """把前端带来的摘要 JSON 解析成结构化数据。

    解析不出来一律当"没有"，绝不让一段坏数据变成模型眼里的学习进度。
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    book = data.get("book")
    if not isinstance(book, str) or not book.strip():
        return None
    out: StudyContext = {"book": book.strip(), "mode": str(data.get("mode") or "plan")}
    for key in ("due", "newTotal", "newDone", "reviewTotal", "reviewDone"):
        v = data.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            out[key] = v  # type: ignore[literal-required]
    hard = data.get("hardWords")
    if isinstance(hard, list):
        words = [str(w) for w in hard if isinstance(w, str) and w.strip()][:5]
        if words:
            out["hardWords"] = words
    return out


def _context_fragments():
    """每个会话共用的上下文片段：今天日期 + 记忆目录 + 只读学习状态摘要。

    三个问「当前是谁」的回调一律走上下文变量（`current_*`），不是读全局镜像：
    两个会话同时在跑时，全局镜像只会指向最后被激活的那个，拿它渲染别人的进度就是串台。
    """
    assert state.memory_injector is not None
    return [
        env_date_fragment(),
        state.memory_injector.fragment,
        study_fragment(lambda: _current_study_context()),
        attachments_fragment(lambda: _current_attachments()),
    ]


# ---------- 会话池：一会话一份私有状态 ----------
#
# 为什么不是"一个当前会话槽位"：槽位模型下，两个标签页会互相把对方的本子换掉
# （写错本子不报错，回头根本查不到），只能靠一把全局锁让它们排队——**同一时刻只跑一个**。
# 池模型下每个会话有自己的流水、自己的循环、自己的闸：A 在跑不影响 B，只有**同一个会话**
# 内部才排队。代价是要自己管生命周期（上限 + 淘汰），见 `_pool_trim`。
#
# 「这一轮属于谁」用上下文变量传，不用全局变量：工具里的 session_id_provider /
# history_provider 必须看到**自己那一轮**的会话，而不是"最后被激活的那个"。
# asyncio 的子任务会继承创建时的上下文，所以请求协程里 set 一次，
# 它派生的工具并发任务、后台提炼任务都能读到同一个值。

_current_session: ContextVar[SessionRuntime | None] = ContextVar(
    "markr_web_current_session", default=None
)


def current_session_id() -> str | None:
    """工具用：本轮会话 id。没绑定时退回镜像，兼容直接跑 loop 的旧调用方。"""
    session = _current_session.get()
    return session.session_id if session is not None else state.session_id


def current_history() -> SessionHistory | None:
    """工具用：本轮会话的流水句柄（按编号取回旧成品靠它）。"""
    session = _current_session.get()
    return session.history if session is not None else state.history


def _current_study_context() -> StudyContext | None:
    """本轮的学习状态摘要：会话私有，绝不读全局镜像（否则排队时读到别人的进度）。"""
    session = _current_session.get()
    return session.study_context if session is not None else state.study_context


def _current_attachments() -> list[dict[str, str]]:
    """本轮用户上传的图片（文件名 + image_id）：同样会话私有。"""
    session = _current_session.get()
    return session.pending_attachments if session is not None else state.pending_attachments


def _sync_mirror(session: SessionRuntime) -> None:
    """把「最近活跃会话」镜像同步成 session（不绑定上下文）。

    镜像只服务「不知道会话是谁」的老接口（列会话、兼容测试台）；真正干活一律用
    上下文变量。两者只在 `_activate` / 本函数里改，别在路由里手改。
    """
    state.session_id = session.session_id
    state.history = session.history
    state.loop = session.loop
    state.study_context = session.study_context
    state.pending_attachments = session.pending_attachments
    state.session_gate = session.gate


def _activate(session: SessionRuntime):
    """把这一轮绑到某个会话 + 同步镜像。返回可回滚的 token（请求结束要 reset）。"""
    token = _current_session.set(session)
    _sync_mirror(session)
    return token


def _deactivate(token) -> None:
    """解绑。用 reset 而不是 set(None)：请求可能横跨多个上下文边界。"""
    _current_session.reset(token)


def _latest_session() -> SessionRuntime | None:
    """池里最近用过的会话（OrderedDict 尾部）；池空返回 None。"""
    if not state.sessions:
        return None
    return state.sessions[next(reversed(state.sessions))]


def _pool_get(session_id: str) -> SessionRuntime | None:
    """从池里取会话，并把它挪到 LRU 尾部（= 刚用过）。"""
    session = state.sessions.get(session_id)
    if session is not None:
        state.sessions.move_to_end(session_id)
    return session


def _pool_put(session: SessionRuntime) -> SessionRuntime:
    """把会话放进池里，再按上限收敛。

    **同 id 已在池里时不许静默覆盖**：覆盖会造出"同一个会话两套流水、两把闸"，
    串行化就静默失效了（写错账本不报错）。所以：
      * 在用的那个 —— 保留旧的、丢弃新的（调用方拿到的是旧对象，继续用同一套状态）；
      * 空闲的那个 —— 关掉旧的再换新的（否则文件句柄泄漏，Windows 上还删不掉文件）。
    """
    existing = state.sessions.get(session.session_id)
    if existing is not None and existing is not session:
        if existing.in_use():
            logger.warning("会话池：%s 还在用，不覆盖（沿用已有运行时）", session.session_id)
            return existing
        existing.close()
    state.sessions[session.session_id] = session
    state.sessions.move_to_end(session.session_id)
    _pool_trim()
    return session


def _pool_trim() -> None:
    """按 LRU 收敛到上限。

    **绝不淘汰在用的会话**（在跑 / 排队中 / 已取到还没开跑）——宁可短暂超限并在日志里
    说清楚，也不去掐别人的对话：掐了会留下半截账本，比多占几 MB 内存严重得多。
    """
    limit = (
        state.settings.session_pool_max if state.settings is not None else DEFAULT_SESSION_POOL_MAX
    )
    while len(state.sessions) > max(limit, 1):
        victim = next((sid for sid, s in state.sessions.items() if not s.in_use()), None)
        if victim is None:
            logger.warning(
                "会话池：%d 个会话都在用，暂不淘汰（上限 %d）", len(state.sessions), limit
            )
            return
        _pool_evict(victim)


def _pool_evict(session_id: str) -> None:
    """从池里拿掉一个会话：关流水句柄、清复核账本。

    **不删磁盘上的流水**：淘汰只为省内存，删文件是另一件事（会话删除接口干那个）。
    """
    session = state.sessions.pop(session_id, None)
    if session is None:
        return
    was_mirror = state.session_id == session_id
    try:
        session.close()
    except Exception as exc:  # noqa: BLE001 - 收尾失败不该把一个不相干的请求打成 500
        logger.warning("会话池：关闭 %s 的流水失败：%s", session_id, exc)
    state.recheck_ledger.pop(session_id, None)
    if was_mirror:
        # 镜像刚落空了：退到池里最近用过的那个；池也空了就置 None（接口会答 503）
        fallback = _latest_session()
        if fallback is not None:
            _sync_mirror(fallback)
        else:
            state.session_id = None
            state.history = None
            state.loop = None


def _session_busy(session_id: str) -> bool:
    """这个会话现在有没有人在用（删除 / 改配置前的忙检查统一走这里）。

    池里有就按池里的判据；池里没有但正好是镜像指向的那个，退回看镜像的循环。
    以前三处都读 `state.loop.busy`——那只代表"最后活跃的那个会话"，别的会话在跑时
    它照样答"不忙"，删除接口就会把正在写的本子关掉。
    """
    session = state.sessions.get(session_id)
    if session is not None:
        return session.in_use()
    return session_id == state.session_id and state.loop is not None and state.loop.busy


def _make_session(history: SessionHistory) -> SessionRuntime:
    """给一个流水装配出会话运行时（唯一入口：所有新建 / 打开路径共用）。"""
    assert state.settings is not None and state.llm is not None and state.registry is not None
    return SessionRuntime(
        history=history,
        loop=_make_loop(
            state.settings,
            state.llm,
            state.registry,
            history,
            compactor=state.compactor,
            compaction_observer=state.compaction_observer,
        ),
        session_id=history.session_id,
        # 提炼调度器一会话一份：全局一份的话，两个会话的轮数会累加到同一个"攒够 5 轮"
        # 计数上（提前触发），并发触发时后到的那次还会被直接丢掉。
        scheduler=_make_extract_scheduler(),
    )


def _make_extract_scheduler() -> ExtractScheduler | None:
    """按当前配置造一个提炼调度器（纯逻辑对象，没有 IO，会话各持一份没成本）。"""
    settings = state.settings
    if settings is None:
        return None
    return ExtractScheduler(
        every_turns=settings.memory_extract_every_turns,
        idle_seconds=settings.memory_extract_idle_seconds,
    )


def _session_from_mirror() -> SessionRuntime | None:
    """把镜像字段拼成一个会话运行时（兼容路径：老调用方没给 id、池还是空的）。

    闸直接复用 `state.session_gate` 而不是新造一把：调用方（含测试）会先占住这条闸
    再发请求，只有同一条闸才验得出来「排队」这件事。
    """
    if state.history is None or state.loop is None:
        return None
    return SessionRuntime(
        history=state.history,
        loop=state.loop,
        session_id=state.session_id or "",
        study_context=state.study_context,
        pending_attachments=list(state.pending_attachments),
        gate=state.session_gate,
    )


async def _ensure_session(sid: str | None) -> SessionRuntime:
    """按请求要的会话 id 取运行时：池里有就用，没有就打开，打不开就新建。

    与老的「翻槽位」不同，这里**不关**任何别的会话——它们留在池里继续可用。
    """
    if not sid:
        bound = _current_session.get()
        if bound is not None:
            return bound
        latest = _latest_session()
        if latest is not None:
            return latest
        mirrored = _session_from_mirror()
        if mirrored is not None:
            return _pool_put(mirrored)
        return _pool_put(_make_session(await _new_history()))
    bound = _current_session.get()
    if bound is not None and bound.session_id == sid:
        return bound
    cached = _pool_get(sid)
    if cached is not None:
        return cached
    _safe_session_id(sid)  # 防路径穿越；形状不对是 404，不当成"打不开"
    try:
        # 与打开会话接口同一套入参：漏了 context 会让这个会话丢掉
        # 日期 / 记忆目录 / 学习状态三个上下文片段。
        history = await open_session(
            sid,
            system_prompt=state.system_prompt,
            context=_context_fragments(),
        )
    except HTTPException:
        raise  # 形状校验的拒绝照原样抛，绝不静默换个新会话糊过去
    except Exception:
        # 打不开（不存在 / 损坏 / 磁盘被清）一律开新的，绝不留着别人的会话继续写
        history = await _new_history()
    return _pool_put(_make_session(history))


async def _run_memory_extraction(
    history: SessionHistory,
    scheduler: ExtractScheduler,
    session: SessionRuntime | None = None,
) -> None:
    """后台跑一次提炼。不阻塞对话；失败只记日志（D27）。

    ``session`` 给了就把任务句柄清在它身上（会话私有）；不给则退回全局字段（兼容旧调用）。
    """
    from glyph.observability import brief  # 局部导入：避免顶层多一个循环依赖面

    extractor = state.memory_extractor
    if extractor is None:
        scheduler.finish(saved=False)
        state.extract_task = None
        if session is not None:
            session.extract_task = None
        return
    try:
        transcript = _recent_transcript(history)
        titles = [entry.title for entry in _memory_store().list_entries()]
        result = await extractor.run(
            session_id=history.session_id,
            title=_session_title(history),
            transcript=transcript,
            day=datetime.now().date(),
            existing_titles=titles,
        )
        scheduler.finish(saved=result.saved)
        if result.saved:
            logger.info("记忆提炼：存下 %d 条（%s）", result.saved_count, result.path)
        elif result.error:
            logger.warning("记忆提炼失败：%s", brief(result.error))
        else:
            logger.info("记忆提炼：这次没有值得记的东西")
    except asyncio.CancelledError:
        scheduler.finish(saved=False)
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务，绝不炸主流程
        scheduler.finish(saved=False)
        logger.warning("记忆提炼异常：%s", brief(exc))
    finally:
        state.extract_task = None
        if session is not None:
            session.extract_task = None


def _memory_store() -> MemoryStore:
    """记忆库实例（与注入器 / 工具共用同一个根）。"""
    return MemoryStore(root=memory_root())


def _recent_transcript(history: SessionHistory) -> list[dict]:
    """把最近这一段会话流水转成提炼器要的材料（角色 + 纯文本）。"""
    out: list[dict] = []
    for message in history.messages():
        role = str(getattr(message, "role", "") or "")
        texts: list[str] = []
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                texts.append(str(text))
        if not texts:
            continue
        out.append({"role": role, "text": " ".join(texts)})
    return out[-40:]  # 只带最近 40 条，别把整场对话塞进提炼提示词


def _session_title(history: SessionHistory) -> str:
    """会话标题：取首条用户消息前若干字，给暂存文件名用。"""
    for message in history.messages():
        if str(getattr(message, "role", "")) != "user":
            continue
        for block in getattr(message, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                return str(text).strip()[:30] or "未命名"
    return "未命名"


def _tick_memory_scheduler(*, had_prose: bool, wrote_memory: bool = False) -> None:
    """一轮结束时问调度器要不要提炼；要就丢到后台，不阻塞对话。

    调度器与提炼任务都是**会话私有**的：全局一份的话，两个会话的轮数会往同一个
    "攒够 5 轮"计数上累加（提前触发），并发触发时后到的那次还会被直接丢掉。
    """
    session = _current_session.get()
    if session is None:
        return
    scheduler = session.scheduler
    history = session.history
    if scheduler is None or history is None:
        return
    decision = scheduler.observe(
        TurnSample(
            at=datetime.now(),
            has_user_prose=had_prose,
            already_wrote_memory=wrote_memory,
        )
    )
    logger.info(
        "记忆调度：%s（会话 %s 攒了 %d 轮，游标 %d）",
        decision.value,
        session.session_id,
        scheduler.pending_turns(),
        scheduler.cursor(),
    )
    if decision is not ExtractDecision.EXTRACT:
        return
    if session.extract_task is not None and not session.extract_task.done():
        logger.info("记忆调度：这个会话上一次提炼还没跑完，这次先不跑")
        return
    session.extract_task = asyncio.create_task(_run_memory_extraction(history, scheduler, session))


def _switch_session(new_history: SessionHistory) -> None:
    """把一个流水变成「当前会话」（切会话 / 兜底恢复 / 新建都走这里）。

    与旧实现的区别：**不再关掉上一段的流水**。池模型下上一个会话仍然活着、仍然可切回去，
    关掉它等于把别人的会话从内存里掐了。真正要销毁走 `_pool_evict`。
    """
    # 切换会话 = 上一个会话这一段结束：把它攒着的材料立刻收尾（D27 兜底）。
    previous = _current_session.get() or _latest_session()
    if previous is not None and previous.scheduler is not None:
        if previous.scheduler.flush_on_session_switch() is ExtractDecision.EXTRACT:
            if state.memory_extractor is not None:
                if previous.extract_task is None or previous.extract_task.done():
                    previous.extract_task = asyncio.create_task(
                        _run_memory_extraction(previous.history, previous.scheduler, previous)
                    )
        previous.scheduler.reset_for_new_session()
    session = _pool_put(_make_session(new_history))
    _sync_mirror(session)
    # §6.3：切换会话时服务端自动重置复核账本（新工作流从 0 计数）
    state.recheck_ledger.pop(new_history.session_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程启动时装配运行时 + 开一个会话，关闭时 flush 落盘。"""
    state.settings = load_settings()
    # 日志接线（设计稿 §3.3）：配置只在入口显式调用，库代码一律不自动配 ——
    # 这是现有 1800+ 测试行为不变的关键（import glyph 无副作用）。
    configure_logging(level=state.settings.log_level, log_file=state.settings.log_file)
    # 回收站定期清理：只删 .trash 里超过 30 天的文件，不碰正在生效的记忆。
    try:
        MemoryStore(root=memory_root()).purge_trash(older_than_days=30)
    except Exception:  # noqa: BLE001 - 清理失败不能挡住启动
        logging.getLogger(__name__).warning("清理记忆回收站失败", exc_info=True)
    # 用户在设置页保存的模型来源优先级高于 .env：先恢复落盘配置，再叠加成最终 Settings
    # （坏 / 缺文件自动回默认档，不阻断启动）。读的是 v2 整本配置（多档并存 + 当前启用），
    # v1 老文件由 store 在读时迁移；当前启用那一档换算成运行时认识的形状。
    # 注意用 provider_book()（内部已做凭据水合 + 落定当前档）：直接 load_provider_book()
    # 拿到的是"还没补密钥"的书，当前档会被判成没配好 → 启动后整套跑成内置档
    # （2026-10-28 真机踩到：自定义档的模型全被当成"不支持的模型"）。
    book = provider_book()
    state.provider = to_legacy(book)
    # 迁移缺口补偿：v1 → v2 只把逐能力绑定（看图 / 批改）搬到了自定义档，内置千问那一档
    # 的绑定会丢。当前档没有任何绑定时，用 v1 读者读同一份文件把旧绑定捡回来
    # （文件已是 v2 时 v1 读者读不出 role_models，这里天然是空操作，不会覆盖新绑定）。
    if not state.provider.role_models.vision and not state.provider.role_models.grade:
        state.provider = replace(state.provider, role_models=load_provider_config().role_models)
    state.settings = apply_provider(state.settings, state.provider)
    # 思考档：按"这一档上次选的档位"恢复（重启后不丢）；没记过才用 .env 的默认
    remembered_effort = book.last_efforts.get(active_provider_id(book), "")
    if remembered_effort:
        state.settings = replace(state.settings, reasoning_effort=remembered_effort)
    (
        state.llm,
        state.registry,
        state.system_prompt,
        state.memory_injector,
        state.compactor,
        state.compaction_observer,
        state.extract_scheduler,
        state.memory_extractor,
        state.memory_consolidator,
    ) = _build_runtime(state.settings)
    history = await new_session(
        system_prompt=state.system_prompt,
        model=state.settings.model,
        cwd=os.getcwd(),
        context=_context_fragments(),
    )
    # 走同一条装配路径（_switch_session → _make_session → 进池 → 同步镜像）：
    # 之前这里手工造一次 loop、紧接着再 _pool_put 造第二次，同一个会话就有了两个运行时对象，
    # 镜像指向的那个还没人用——"唯一装配入口"当场破例。
    _switch_session(history)
    try:
        yield
    finally:
        # 退出时把所有会话的流水统一收尾（池里每个都要 flush，不能只关镜像那个）
        for session in list(state.sessions.values()):
            session.close()
        state.sessions.clear()


# ---------- FastAPI ----------


# ---------- 会话管理 ----------


def _assert_runtime_ready() -> None:
    if state.loop is None or state.history is None or state.session_id is None:
        raise HTTPException(status_code=503, detail="运行时未就绪")


async def _open_fresh_session() -> str:
    """当前会话被删掉后开一个新的，并把全局运行时指过去。

    这一步**不能失败到一半**：它一旦抛出，`_history` / `_session_id` 就会停在 None，
    于是所有接口都变成 503「运行时未就绪」，只能重启进程才救得回来。
    """
    assert state.settings is not None
    history = await new_session(
        system_prompt=state.system_prompt,
        model=state.settings.model,
        cwd=os.getcwd(),
        context=_context_fragments(),
    )
    _switch_session(history)
    return history.session_id


async def _salvage_runtime() -> bool:
    """兜底：新会话开不出来时，退回到磁盘上还存在的任意一个会话。

    宁可让界面落到一个旧会话上，也不要停在「运行时未就绪」——
    后者会把整个后端变成死的，而用户看到的只是一句没头没尾的 503。
    """
    assert state.settings is not None
    for summary in iter_session_summaries(limit=1):
        try:
            history = await open_session(
                summary.session_id,
                system_prompt=state.system_prompt,
                context=_context_fragments(),
            )
        except (SessionNotFoundError, SessionCorruptError):
            continue
        _switch_session(history)
        return True
    return False


async def _ensure_current_session(sid: str | None) -> str:
    """兼容入口：按 sid 取会话并把它同步成「当前会话」，返回实际在用的会话 id。

    老调用方（和旧测试）只想要一个 id；新代码请直接用 `_ensure_session` 拿会话对象，
    并用 `_activate` 把这一轮绑上去——只有那样两个会话才能真的并行。
    """
    session = await _ensure_session(sid)
    _sync_mirror(session)
    return session.session_id


async def _new_history() -> SessionHistory:
    """开一个新会话（唯一入口：所有新建路径共用一份装配参数）。"""
    assert state.settings is not None
    return await new_session(
        system_prompt=state.system_prompt,
        model=state.settings.model,
        cwd=os.getcwd(),
        context=_context_fragments(),
    )


def _safe_session_id(session_id: str) -> str:
    """外部进来的会话 id 必须先过形状校验，再交给 session_path 拼路径。

    ``session_path`` 会把 id 原样拼进文件名，形如 ``../../../../evil`` 的 id 能跳出
    会话根目录（读 / 删到不该碰的 .jsonl）。对外一律答"不存在"，不给"格式不对"
    这种可以拿来试探的反馈。
    """
    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return session_id


def _first_user_message(summary) -> str | None:
    """读会话文件，取第一条 user_message 的 text 作为会话标题。

    返回三态：非空 = 标题；"" = 开过口但首条没打字（只发了图）；
    None = 一条 user_message 都没有（从没说过话）。非空标题会缓存，
    避免每次列会话都扫全文件。"""
    sid = summary.session_id
    cached = state.title_cache.get(sid)
    if cached:
        return cached
    try:
        with summary.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "user_message":
                    text = (rec.get("payload") or {}).get("text", "")
                    text = text.strip()
                    if text:
                        state.title_cache[sid] = text
                    return text
    except OSError:
        return ""  # 打不开 ≠ 没说过话：照旧列出（未命名），不许静默藏掉
    return None


# ---------- SSE 保活与错误人话化（前端断线检测 / 额度快速失败靠这层） ----------

_SSE_HEARTBEAT_SECONDS = 15.0
"""长耗时工具（批改 / OCR / 压缩）静默期，每隔这么久发一帧 SSE 注释心跳。

两个用途：① 顶住反向代理 / 浏览器对"长时间无字节连接"的回收；② 给前端的
空闲看门狗当"后端还活着"的信号。注释帧以冒号开头、不是事件，前端解析器天然忽略。
"""

_QUEUE_NOTICE_SECONDS = 1.0
# 切会话时若上一轮还在收尾，最多等这么久再放弃（前端已先发取消，通常一两秒就释放）
_OPEN_SESSION_BUSY_WAIT_SECONDS = 5.0
"""等待"会话闸"（`_session_gate`）时，每隔这么久先给前端吐一帧"已排队"提示（BUG-03）。

闸被上一轮占着时（典型：客户端断连后，旧请求要等下一帧写失败才被回收，最长
`_SSE_HEARTBEAT_SECONDS`），若这段等待一帧不发，用户会以为卡死（2026-10-12 真机实测约 15s
假死）。取闸改为"带超时 + 超时就吐一帧"，保证等待期间始终有可见反馈；拿到闸后绝不重复发。
"""

_SSE_HEARTBEAT = ": ping\n\n"
# 图片后缀（走 ocr 通道，进内存图片仓）；mime 按扩展名推断（浏览器头偶尔是 octet-stream）
# 注意：gif 刻意不在此列（addendum §6.1：动态图取哪帧不可控，与"忠实转写"冲突），
# 由下方专门分支拒绝并给可操作文案；ocr.py::MIME_WHITELIST 本就不含 gif，是第二道防线。
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# 文本后缀（走 read_file 通道，服务端持久化返回 path）；其余一律服务端拒绝
_TEXT_EXTS = {".txt", ".md"}

# txt/md 的落盘目录（read_file 工具按绝对路径消费；测试里 monkeypatch 成 tmp_path）



def provider_file_writable() -> bool:
    """供应商配置文件能不能写（只读部署 → 设置页所有写按钮禁用）。"""
    from glyph.provider.paths import provider_config_path

    path = provider_config_path()
    try:
        if path.exists():
            return os.access(path, os.W_OK)
        return os.access(path.parent, os.W_OK)
    except OSError:
        return False
