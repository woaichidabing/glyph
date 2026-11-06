"""Glyph 内置工具的**统一装配台账**：一处注册全部产品能力。

背景：原先这份"逐行 register"清单埋在 ``web/server.py`` 的 ``_build_runtime`` 里，
而 examples / tests 又各自 ``ToolRegistry()`` 手工接线——"系统到底装了哪些工具"
没有一个一眼可见的家。抽到这里后，web 台 / 桌面 sidecar / 离线装配都走同一个入口，
新增工具只改这一处，注册顺序与工具集合有唯一事实来源。

设计要点（与 ``factory.py`` 的分工）：

- ``factory.py`` 回答"**单个工具怎么造**"：把配置、模型客户端、数据源接成一个工具；
- 本模块回答"**整套产品装哪些、按什么顺序注册**"：逐个 build 后登记进注册表，
  再按实际注册表重建系统提示词（注册即对模型可见，杜绝"注册了但提示词没列"）。

运行期依赖一律经参数注入，工具本身不认识文件系统 / 当前会话（与 factory 的 provider
模式一致）：

- ``image_store`` / ``dict_repo``：图片字节仓、本地 ECDICT 词典仓，由装配层供图；
- ``upload_dirs``：``read_file`` 的目录白名单（只读工具也只能读这些目录）；
- ``session_id_provider`` / ``history_provider``：运行期回调，分别取"当前会话 id"
  （练习台账据此归属）与"当前会话全量流水"（提货单据此取回成品 / 定稿）；
- ``recheck_wrapper``：留给装配层的服务端横切接缝。web 台用它套"单条 / 单篇复核
  配额闸"（账本是 web 层状态，不进 tools/）；demo / 测试传 None 即得到裸工具；
- ``wordbook_store``：用户生词本 / 查词历史（web 台注入同一份单例）；不传时
  收词卡仍可用，只是“查过几次 / 是否已收”按 0 次 / 未收处理。

运行时内核仍只通过注册表接触工具（铁律 R2），本模块只在配置期运行一次。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from ..agent_prompt import build_system_prompt
from ..config import Settings
from ..llm.models import network_capable
from ..memory import MemoryCatalogInjector
from ..plan import PlanStore
from ..runtime.registry import ToolRegistry
from ..runtime.tool import Tool
from ..study import StudyStore
from ..wordbook import WordbookStore
from .artifact import ArtifactReader
from .ask_user import AskUserGateway, AskUserTool
from .dict_repo import DictRepository
from .factory import (
    build_analyze_writing_tool,
    build_attempts,
    build_describe_image_tool,
    build_grade_text_tool,
    build_memory,
    build_ocr_image_tool,
    build_polish_text_tool,
    build_read_artifact_tool,
    build_recheck_detection_tool,
)
from .file_tools import ReadFileTool
from .image import ImageProvider
from .lookup_word import LookupWordTool
from .plan_read import ReadPlanTool
from .plan_tools import ProposePlanTool
from .study_read import ReadStudyTool
from .wordbook_proposal import ProposeWordbookAddTool

__all__ = ["build_default_registry"]


def build_default_registry(
    settings: Settings,
    *,
    image_store: ImageProvider,
    dict_repo: DictRepository,
    upload_dirs: Sequence[Path],
    session_id_provider: Callable[[], str | None],
    history_provider: Callable[[], ArtifactReader | None],
    recheck_wrapper: Callable[[Tool], Tool] | None = None,
    ask_gateway: AskUserGateway | None = None,
    wordbook_store: WordbookStore | None = None,
    plan_store: PlanStore | None = None,
    study_store: StudyStore | None = None,
) -> tuple[ToolRegistry, MemoryCatalogInjector, str]:
    """装配 Glyph 内置工具注册表。

    返回 ``(registry, memory_injector, system_prompt)``：

    - ``registry``：已登记全部产品工具，可直接交给 AgentLoop；
    - ``memory_injector``：长期记忆目录注入器，装配根把它的 ``fragment`` 作为
      上下文片段每轮注入会话（不落盘）；
    - ``system_prompt``：按**实际注册表**重建的系统提示词，保证多注册的工具
      （如记忆三件套）对模型不是"不存在"。

    ``recheck_wrapper`` 非 None 时，用它包裹 recheck 工具再注册（web 台传入配额闸）；
    为 None 时注册裸 recheck 工具（demo / 离线测试）。
    """
    registry = ToolRegistry()

    # 纯文本批改（本地拼写检查 + 模型批改）
    registry.register(build_grade_text_tool(settings))
    # 图片 → 英文文本转写 / 看懂一张图：都依赖一个能接收图像输入的视觉模型。
    # 自定义端点若清单里没有任何视觉模型，apply_provider 会把 vision_model 置空——
    # 此时这两个工具**不注册**（系统提示词据实际注册表生成，也不会列出），agent 不会
    # 拿图去调一个纯文本模型而报错；前端据 /api/config 的能力位禁用图片上传入口。
    if settings.vision_model.strip():
        # 图片 → 英文文本转写（只转写不批改；图片字节由 image_store 按 id 提供，
        # 不把 base64 塞进工具参数，避免撑爆会话 JSONL 与上下文）
        registry.register(build_ocr_image_tool(image_store, settings))
        # 看懂一张图（只看不批）：主模型看不见图时，这条是用户发图问"这是啥"的眼睛
        registry.register(build_describe_image_tool(image_store, settings))
    # 单条复核：装配层可在此套服务端配额闸（单条 / 单篇上限在服务端强制）
    recheck_tool = build_recheck_detection_tool(settings)
    if recheck_wrapper is not None:
        recheck_tool = recheck_wrapper(recheck_tool)
    registry.register(recheck_tool)

    # 长期记忆五件套
    # （remember / record_review_result / confirm_mastery / forget / read_memory）
    # 共享同一个文件 store；写入后下一轮经注入器即可见
    _mem_store, mem_tools, memory_injector = build_memory(settings)
    for mem_tool in mem_tools:
        registry.register(mem_tool)

    # 读取用户上传的 txt/md：只能读 upload_dirs 白名单目录（路径越界 / 后缀均受限）
    registry.register(ReadFileTool(list(upload_dirs)))
    # 本地英汉词典查词（ECDICT，只读、不走模型）
    registry.register(LookupWordTool(dict_repo))
    # 收词确认卡：只查词库 + 读查词次数，不写生词本；用户点卡片后才走 /api/user/wordbook
    registry.register(ProposeWordbookAddTool(dict_repo, wordbook_store))
    # 作文 CEFR 等级分析（硬指标 + 词汇分布确定性算，模型只对照，给区间 + 原文证据）
    registry.register(build_analyze_writing_tool(settings))
    # 表达升档（不改错，只求更好）：升级词的考试标签 / 近似等级查同一个词典仓，
    # 与 lookup_word 共用一份词库数据源，不另开连接
    registry.register(build_polish_text_tool(settings, repo=dict_repo))

    # 练习尝试记录三件套（record / read / delete）：record 经 provider 在运行期
    # 取当前会话 id，切会话自动归属新会话；无活动会话时工具明确报错，不记错地方
    _attempt_store, attempt_tools = build_attempts(
        settings, session_id_provider=session_id_provider
    )
    for attempt_tool in attempt_tools:
        registry.register(attempt_tool)

    # 让 Glyph 规划：把目标拆成结构化学习计划，工具不落库，前端让用户确认后写入本地计划。
    registry.register(ProposePlanTool())
    if plan_store is not None:
        registry.register(ReadPlanTool(plan_store))
    if study_store is not None:
        registry.register(ReadStudyTool(study_store))

    # read_artifact：L2 装箱后模型按编号只读取回旧批改 / 定稿；
    # 读取对象是当前会话流水，经 provider 运行期取，切会话自动指向新会话
    registry.register(build_read_artifact_tool(history_provider=history_provider))

    # 停下来问用户一句（拿不准时才用）：gateway 是界面 ↔ 工具的回答通道，
    # 由装配层持有同一个实例——服务端收到用户答复时，正是经它把答案递给挂起中的工具。
    # 没传（demo / 离线测试）时自建一个，工具照样能注册，只是没人应答。
    registry.register(AskUserTool(ask_gateway or AskUserGateway()))

    # 按实际注册表重建系统提示词：声明的联网能力 = 全局开关 ∩ 所选型号自身能力
    # （web_search 不是每个主模型都支持），工具清单同理——以注册表为唯一来源
    system_prompt = build_system_prompt(
        tools=registry.tools(),
        network_capable=network_capable(settings.model, settings.web_search),
        # 主模型自己能看图时，系统词要改成"你能直接看"——否则明明是看图模型，
        # 提示词却让它说"我看不见"，看图功能当场废掉（2026-10-31 接上）。
        vision_capable=bool(getattr(settings, "main_model_can_see_image", False)),
        style=settings.speaking_style,
    )

    return registry, memory_injector, system_prompt
