"""grade_text 工具的组装工厂。

把配置（Settings）、OpenAI 兼容客户端、批改器、本地拼写器接成一个可直接
注册到 ToolRegistry 的工具。运行时内核不依赖本工厂，它只是给"装配层"
（web / demo / 桌面端）提供一个统一入口，避免各处重复接线。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..attempts import AttemptStore, attempts_root
from ..config import Settings, load_settings
from ..llm.fallback import FallbackChatClient
from ..memory import MemoryCatalogInjector, MemoryStore, memory_root
from .analyze_writing import AnalyzeWritingTool
from .artifact import ArtifactReader, ReadArtifactTool
from .attempt_tools import DeleteAttemptTool, ReadAttemptsTool, RecordAttemptTool
from .describe import DescribeImageTool, LLMDescriber
from .dict_repo import DictRepository, default_dict_path
from .grade import GradeTextTool
from .grader import LLMGrader
from .image import ImageProvider
from .memory_tools import (
    ConfirmMasteryTool,
    ForgetTool,
    ReadMemoryTool,
    RecordReviewResultTool,
    RememberTool,
)
from .ocr import LLMTranscriber, OcrImageTool
from .polish import PolishTextTool
from .polish_analyzer import LLMPolisher
from .recheck import LLMRechecker, RecheckDetectionTool
from .spell import PySpellingChecker
from .writing_analyzer import LLMWritingAnalyzer

__all__ = [
    "build_grade_text_tool",
    "build_ocr_image_tool",
    "build_describe_image_tool",
    "build_recheck_detection_tool",
    "build_memory",
    "build_attempts",
    "build_analyze_writing_tool",
    "build_polish_text_tool",
    "build_read_artifact_tool",
]


def build_grade_text_tool(settings: Settings | None = None, *, client=None) -> GradeTextTool:
    """构造 grade_text 工具。

    - settings 缺省走 load_settings()（读 .env / 环境变量）；
    - client 缺省时用 settings 里的 key/base_url 自建 AsyncOpenAI（仅构造，不发请求），
      便于测试注入假客户端。
    """
    settings = settings or load_settings()

    if client is None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError(
                "grade_text 需要 openai 客户端，请安装 markr 的 qwen 或 grade 额外依赖"
            ) from exc
        client = openai.AsyncOpenAI(
            api_key=settings.api_key or "missing-api-key",
            base_url=settings.base_url,
            timeout=120.0,  # 批改是一次性长输出，给足时间
        )
        # 首选批改模型没额度/没权限时，自动换同端点的降级模型再试（见 llm/fallback.py）
        client = FallbackChatClient(
            client,
            primary=settings.grade_model,
            fallbacks=settings.grade_fallback_models,
        )

    grader = LLMGrader(
        client,
        settings.grade_model,
        max_tokens=settings.max_tokens,
        max_tokens_upgrade=settings.max_tokens_upgrade,
    )
    spell = PySpellingChecker()
    return GradeTextTool(grader=grader, spell=spell)


def build_ocr_image_tool(
    provider: ImageProvider,
    settings: Settings | None = None,
    *,
    client=None,
) -> OcrImageTool:
    """构造 ocr_image 工具（只转写，不批改）。

    - ``provider`` **必填**：工具不认识文件系统，图片字节由装配层（web / 桌面端）按
      ``image_id`` 提供；这样也避免把 base64 塞进工具参数（会撑爆会话 JSONL 与上下文）；
    - settings 缺省走 ``load_settings()``；client 缺省时用 settings 的 key/base_url 自建
      AsyncOpenAI（仅构造，不发请求），便于测试注入假客户端。
    """
    settings = settings or load_settings()

    if client is None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError(
                "ocr_image 需要 openai 客户端，请安装 markr 的 qwen 或 grade 额外依赖"
            ) from exc
        client = openai.AsyncOpenAI(
            api_key=settings.api_key or "missing-api-key",
            base_url=settings.base_url,
            timeout=120.0,  # 视觉转写是一次性输出，给足时间
        )

    transcriber = LLMTranscriber(client, settings.vision_model)
    return OcrImageTool(
        provider=provider,
        transcriber=transcriber,
        max_bytes=settings.ocr_max_bytes,
    )


def build_describe_image_tool(
    provider: ImageProvider,
    settings: Settings | None = None,
    *,
    client=None,
) -> DescribeImageTool:
    """构造 describe_image 工具（看懂一张图，只看不批）。

    与 ``build_ocr_image_tool`` 对称：复用同一个 ``vision_model``（图都靠它看），
    client 缺省时用 settings 的 key/base_url 自建 AsyncOpenAI（仅构造，不发请求）。

    它和 ocr_image 走同一个视觉模型，但**契约不同**：ocr_image 产出英文原文给批改用，
    describe_image 产出中文描述给"用户发图问这是啥"用。
    """
    settings = settings or load_settings()

    if client is None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError(
                "describe_image 需要 openai 客户端，请安装 markr 的 qwen 或 grade 额外依赖"
            ) from exc
        client = openai.AsyncOpenAI(
            api_key=settings.api_key or "missing-api-key",
            base_url=settings.base_url,
            timeout=120.0,
        )

    describer = LLMDescriber(client, settings.vision_model)
    return DescribeImageTool(
        provider=provider,
        describer=describer,
        max_bytes=settings.ocr_max_bytes,
    )


def build_recheck_detection_tool(
    settings: Settings | None = None, *, client=None
) -> RecheckDetectionTool:
    """构造 recheck_detection 工具（使用者驳回一条后只复核这一处）。

    与 ``build_grade_text_tool`` 对称：复用同一个 ``grade_model``，client 缺省时自建
    AsyncOpenAI（仅构造，不发请求），便于测试注入假客户端。
    """
    settings = settings or load_settings()

    if client is None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError(
                "recheck_detection 需要 openai 客户端，请安装 markr 的 grade 额外依赖"
            ) from exc
        client = openai.AsyncOpenAI(
            api_key=settings.api_key or "missing-api-key",
            base_url=settings.base_url,
            timeout=60.0,  # 复核是短输出，不需要批改那样的 120 秒
        )
        # 复核与批改共用 grade_model，同样需要"没额度就换模型"的兜底
        client = FallbackChatClient(
            client,
            primary=settings.grade_model,
            fallbacks=settings.grade_fallback_models,
        )

    rechecker = LLMRechecker(client, settings.grade_model)
    return RecheckDetectionTool(
        rechecker=rechecker,
        max_rounds=settings.recheck_max_rounds,
        min_context_words=settings.recheck_min_context_words,
        max_context_chars=settings.recheck_max_context_chars,
    )


def build_memory(
    settings: Settings | None = None,
    *,
    root: Path | None = None,
) -> tuple[MemoryStore, list, MemoryCatalogInjector]:
    """一次接好长期记忆：共享 store + 三个记忆工具 + 目录注入器。

    - root 缺省时走 ``memory_root()``（环境变量 GLYPH_MEMORY_ROOT 可覆盖）；
    - 三个工具与注入器共用**同一个 store**：写入后下一轮注入立刻可见；
    - 装配根把 ``injector.fragment`` 作为上下文片段传进会话，
      再把三个工具注册进注册表即可。
    """
    settings = settings or load_settings()
    base = memory_root() if root is None else root
    store = MemoryStore(base)

    tools = [
        RememberTool(store, title_max_chars=settings.memory_title_max_chars),
        RecordReviewResultTool(store),
        ConfirmMasteryTool(store),
        ForgetTool(store),
        ReadMemoryTool(store, body_max_bytes=settings.memory_body_max_bytes),
    ]
    injector = MemoryCatalogInjector(
        store,
        max_entries=settings.memory_catalog_max_entries,
        fact_unused_days=settings.memory_fact_unused_days,
        line_max_chars=settings.memory_line_max_chars,
        whole_max_bytes=settings.memory_catalog_max_bytes,
    )
    return store, tools, injector


def build_attempts(
    settings: Settings | None = None,
    *,
    root: Path | None = None,
    session_id_provider=None,
) -> tuple[AttemptStore, list]:
    """一次接好学习尝试记录（T07）：共享 store + record/read/delete 三工具。

    - root 缺省走 ``attempts_root()``（环境变量 GLYPH_ATTEMPTS_ROOT 可覆盖）；
    - 三工具共用同一个 store；record 靠 ``session_id_provider`` 在运行期取当前会话
      （装配根传一个返回活动会话 id 的回调；缺省返回 None，此时 record 会明确报错而不是
      把尝试记到来历不明的会话）；
    - read 是跨会话的长期读取，不绑定当前会话 id。
    """
    settings = settings or load_settings()
    base = attempts_root() if root is None else root
    store = AttemptStore(base)
    provider = session_id_provider if session_id_provider is not None else (lambda: None)

    tools = [
        RecordAttemptTool(store, session_id_provider=provider),
        ReadAttemptsTool(
            store,
            read_limit=settings.attempts_read_limit,
            total_max_bytes=settings.attempts_total_max_bytes,
        ),
        DeleteAttemptTool(store),
    ]
    return store, tools


def build_read_artifact_tool(
    *, history_provider: Callable[[], ArtifactReader | None]
) -> ReadArtifactTool:
    """构造 read_artifact（§8.4，#64）：从当前会话全量流水只读取回成品 / 定稿。

    registry 跨会话复用，而读取对象是"当前会话"的流水——故与 build_attempts 的
    session_id_provider 同模式，注入一个运行期返回当前 SessionHistory 的回调；
    切会话后回调自动指向新会话，尚无活动会话时工具给友好报错。
    """
    return ReadArtifactTool(history_provider=history_provider)


def build_analyze_writing_tool(
    settings: Settings | None = None, *, client=None
) -> AnalyzeWritingTool:
    """构造 analyze_writing 工具（证据驱动的 CEFR 等级分析）。

    复用 ``grade_model``（等级对照是重判断的活，与批改同档模型）；client 缺省时按
    settings 的 key/base_url 自建 AsyncOpenAI（仅构造、不发请求），便于测试注入假 client。
    词库路径由 AnalyzeWritingTool 自己走 default_dict_path()。
    """
    settings = settings or load_settings()

    if client is None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError(
                "analyze_writing 需要 openai 客户端，请安装 markr 的 grade 额外依赖"
            ) from exc
        client = openai.AsyncOpenAI(
            api_key=settings.api_key or "missing-api-key",
            base_url=settings.base_url,
            timeout=120.0,
        )
        # 写作分析与批改共用 grade_model，同样需要"没额度就换模型"的兜底
        client = FallbackChatClient(
            client,
            primary=settings.grade_model,
            fallbacks=settings.grade_fallback_models,
        )

    analyzer = LLMWritingAnalyzer(client, settings.grade_model)
    return AnalyzeWritingTool(analyzer)


def build_polish_text_tool(
    settings: Settings | None = None,
    *,
    client=None,
    repo: DictRepository | None = None,
) -> PolishTextTool:
    """构造 polish_text 工具（英文表达升档，只升不改错）。

    用**独立的** ``polish_model``（默认 ``qwen3.7-plus``，**刻意不与批改共用**——
    真机 A/B 见设计文档 §12：flash 系在"本来就写得好"的文本上会谄媚），
    降级链仍复用 ``grade_fallback_models``；client 缺省时按 settings 的
    key/base_url 自建 AsyncOpenAI（仅构造、不发请求），便于测试注入假 client。

    ``repo`` 是本地词库仓——**升级词的考试标签与近似等级全靠它查**，模型不参与定级。
    装配层传入与 ``lookup_word`` 共享的实例；缺省时自建（单独使用 / 测试）。
    """
    settings = settings or load_settings()

    if client is None:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError(
                "polish_text 需要 openai 客户端，请安装 markr 的 grade 额外依赖"
            ) from exc
        client = openai.AsyncOpenAI(
            api_key=settings.api_key or "missing-api-key",
            base_url=settings.base_url,
            timeout=120.0,  # 升档是整段一次性输出，给足时间
        )
        # 润色**不用** grade_model：真机 A/B（设计文档 §12）证明两者合格线不同
        # （flash 系会谄媚），故走独立的 polish_model；兜底链仍复用批改那条。
        client = FallbackChatClient(
            client,
            primary=settings.polish_model,
            fallbacks=settings.grade_fallback_models,
        )

    polisher = LLMPolisher(
        client,
        settings.polish_model,
        max_tokens=settings.max_tokens,
        max_tokens_upgrade=settings.max_tokens_upgrade,
    )
    if repo is None:
        repo = DictRepository(default_dict_path())
    return PolishTextTool(polisher=polisher, repo=repo)
