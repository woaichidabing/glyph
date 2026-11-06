"""自定义上游模型的能力推断。

标准 OpenAI 兼容 ``GET /models`` 只回 ``{id, object, created, owned_by}``，**不带任何
能力信息**（上下文 / 工具调用 / 思考档 / 看图都没有）。这里按模型 id 做保守启发式：

- 能高置信确定的才给能力（看图宁漏勿滥、思考档只给兼容层确实认 ``reasoning`` 的家族、
  窗口宁小勿大），判不准一律走安全默认——绝不替陌生模型下发它不认的参数（会直接 400，
  连话都说不了）；
- 内置实测表（:mod:`glyph.llm.models` 的 ``MAIN_MODELS``）永远优先，推断只用于自定义
  清单里的未知 id。

规则是"名字 → 能力"的单点台账，遇到新家族在这里加规则即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.models import DEFAULT_CONTEXT_WINDOW, REASONING_LEVELS, main_model

# 未知型号的保守兜底窗口（复用 llm.models 的 128k，单一事实源）。
DEFAULT_INFERRED_CONTEXT = DEFAULT_CONTEXT_WINDOW

# 品牌（前端据此选 logo）
BRAND_GENERIC = "generic"
BRAND_OPENAI = "openai"
BRAND_ANTHROPIC = "anthropic"
BRAND_GOOGLE = "google"
BRAND_DEEPSEEK = "deepseek"
BRAND_QWEN = "qwen"
BRAND_ZHIPU = "zhipu"
BRAND_KIMI = "kimi"
BRAND_XAI = "xai"
BRAND_STEPFUN = "stepfun"
BRAND_META = "meta"
BRAND_MISTRAL = "mistral"
BRAND_MINIMAX = "minimax"

# OpenAI gpt / o 系列要求 gpt、o1/o3/o4 前是串首或分隔符，避免误吞 omni、open 之类。
_OPENAI_RE = re.compile(r"(^|[-_/.])gpt|(^|[-_/.])o[134](\d)?($|[-_/.])")
_STEP_RE = re.compile(r"(^|[-_/.])step[-_0-9.]")
# 视觉模型的 -vl / /vl 边界（qwen-vl、qwen2.5-vl、kimi-vl、minimax-vl 等）。
_VL_BOUNDARY_RE = re.compile(r"(^|[-_/.])vl($|[-_/.0-9])")
# 智谱视觉：glm-4v / glm-4.5v / glm-4.6v。
_GLM_V_RE = re.compile(r"glm-?4(\.\d+)?v")
# 机器翻译 -mt-、BAAI bge 嵌入 / 重排系列。
_MT_RE = re.compile(r"(^|[-_/.])mt[-_]")
_BGE_RE = re.compile(r"(^|[-_/.])bge([-_]|$)")
# test- 前缀的内部测试模型（正式发布模型不会以 test- 命名）。
_TEST_RE = re.compile(r"(^|[-_/.])test[-_]")

# 非对话模型（画图 / 语音 / 嵌入 / 翻译 / 实时 / 重排 / 视频生成…）不能当大脑：
# 它们不吃 function 工具，选了主循环直接废。视觉"对话"模型（vl / omni-flash）不在此列。
_NON_CHAT_SUBSTR = (
    "image",
    "dall-e",
    "sora",
    "tts",
    "speech",
    "whisper",
    "voice",
    "audio",
    "asr",
    "music",
    "embedding",
    "rerank",
    "realtime",
    "livetranslate",
    "translate",
    "qwen-mt",
    "ocr",
    "caption",
    "flux",
    "stable-diffusion",
    "cogvideo",
    "text-to-video",
    "image-to-video",
    "-video",
    # omni-turbo 是偏实时的全模态模型，标准 function 工具支持存疑，不当大脑；
    # 较新的通用多模态对话款 omni-flash 保留（能对话、能看图）。
    "omni-turbo",
)


@dataclass(frozen=True)
class ModelCapabilities:
    """一个模型 id 推断出的能力位。"""

    model_id: str
    brand: str = BRAND_GENERIC
    # 标称上下文窗口（token），用量环分母；未知保守 128k。
    context_window: int = DEFAULT_INFERRED_CONTEXT
    # 支持的思考档；空元组 = 不显示思考强度、也不下发 reasoning.effort。
    reasoning_levels: tuple[str, ...] = ()
    # 能否接受图像输入（拍照 OCR / 作文批改看图）；图像"生成"模型不算。
    supports_vision: bool = False
    # 能否当 agent 大脑（对话 + function 工具调用的硬门槛）。
    can_be_main: bool = True

    @property
    def supports_tools(self) -> bool:
        """能当大脑就意味着要能调工具；非对话模型两者一起 False。"""
        return self.can_be_main


def infer_brand(model_id: str) -> str:
    """按模型 id 判品牌（logo 族）；认不出归 generic。"""
    m = model_id.strip().lower()
    if "claude" in m or "anthropic" in m:
        return BRAND_ANTHROPIC
    if "gemini" in m or "gemma" in m:
        return BRAND_GOOGLE
    if "deepseek" in m:
        return BRAND_DEEPSEEK
    if "glm" in m or "zhipu" in m or "chatglm" in m:
        return BRAND_ZHIPU
    if "qwen" in m or "qwq" in m or "qvq" in m or "tongyi" in m:
        return BRAND_QWEN
    if "kimi" in m or "moonshot" in m:
        return BRAND_KIMI
    if "grok" in m or "x-ai" in m or "xai" in m:
        return BRAND_XAI
    if "minimax" in m:
        return BRAND_MINIMAX
    if "stepfun" in m or _STEP_RE.search(m):
        return BRAND_STEPFUN
    if "mixtral" in m or "mistral" in m:
        return BRAND_MISTRAL
    if "llama" in m:
        return BRAND_META
    if _OPENAI_RE.search(m) or any(
        k in m for k in ("chatgpt", "openai", "dall-e", "sora", "whisper", "tts")
    ):
        return BRAND_OPENAI
    return BRAND_GENERIC


def _is_non_chat(m: str) -> bool:
    if any(k in m for k in _NON_CHAT_SUBSTR):
        return True
    return bool(_MT_RE.search(m) or _BGE_RE.search(m) or _TEST_RE.search(m))


def _infer_vision(m: str) -> bool:
    # 宁漏勿滥：只给高置信视觉家族，纯文本模型被误判会一发图就报错。
    if re.search(r"(^|[-_/.])gpt-4o", m) or re.search(r"(^|[-_/.])gpt-5", m):
        return True
    if "claude-3" in m or "claude-4" in m or "gemini" in m:
        return True
    if _VL_BOUNDARY_RE.search(m) or _GLM_V_RE.search(m):
        return True
    if any(k in m for k in ("internvl", "cogvlm", "llava", "mllama", "qvq", "vision", "visual")):
        return True
    # omni 全模态（omni-flash）能看图；omni-realtime 已在非对话阶段被排除。
    if "omni" in m and "realtime" not in m:
        return True
    if "step-1v" in m or "step-1.5v" in m:
        return True
    return False


def _infer_reasoning(m: str) -> tuple[str, ...]:
    # 只给高置信、且 OpenAI 兼容 chat 层确实认 reasoning.effort 的家族：
    # 千问 qwen3 / qwq（真机验证）、OpenAI gpt-5 / o 系。其余（claude / gemini /
    # deepseek-reasoner 等）思考参数各家不一，保守不显示、不下发。
    if "qwen3" in m or "qwq" in m:
        return REASONING_LEVELS
    if re.search(r"(^|[-_/.])gpt-5", m) or re.search(r"(^|[-_/.])o[134](\d)?($|[-_/.])", m):
        return REASONING_LEVELS
    return ()


def _infer_context(m: str) -> int:
    # 宁小勿大：报小了只是更早压缩，报大了会超出真实窗口把请求打爆。
    if "claude" in m:
        return 200_000
    if "gemini" in m:
        return 1_000_000
    if "qwen3" in m or "qwq" in m:
        return 1_000_000
    return DEFAULT_INFERRED_CONTEXT


def infer_capabilities(model_id: str) -> ModelCapabilities:
    """对不在内置实测表里的自定义模型 id 做能力推断。"""
    mid = model_id.strip()
    m = mid.lower()
    brand = infer_brand(mid)
    if _is_non_chat(m):
        # 画图 / 语音 / 嵌入等：不当大脑、不算看图（它们是"出图"不是"看图"），
        # 其余能力位给安全空值，窗口仅占位。
        return ModelCapabilities(
            model_id=mid,
            brand=brand,
            context_window=DEFAULT_INFERRED_CONTEXT,
            reasoning_levels=(),
            supports_vision=False,
            can_be_main=False,
        )
    return ModelCapabilities(
        model_id=mid,
        brand=brand,
        context_window=_infer_context(m),
        reasoning_levels=_infer_reasoning(m),
        supports_vision=_infer_vision(m),
        can_be_main=True,
    )


def capabilities_for(model_id: str) -> ModelCapabilities:
    """能力查询的统一入口：内置实测表优先，未知 id 才走启发式推断。"""
    mid = model_id.strip()
    built = main_model(mid)
    if built is not None:
        # 内置表都是千问端点上的文本主脑：看图由独立 vision 子模型负责，主脑不标视觉。
        return ModelCapabilities(
            model_id=built.id,
            brand=infer_brand(built.id),
            context_window=built.context_window,
            reasoning_levels=built.reasoning_levels,
            supports_vision=False,
            can_be_main=True,
        )
    return infer_capabilities(mid)


def capabilities_payload(model_id: str) -> dict:
    """给前端的能力字典（#67 模型选择器据此显隐思考强度 / 上下文 / 看图入口）。"""
    cap = capabilities_for(model_id)
    return {
        "id": cap.model_id,
        "brand": cap.brand,
        "context_window": cap.context_window,
        "reasoning_levels": list(cap.reasoning_levels),
        "supports_vision": cap.supports_vision,
        "can_be_main": cap.can_be_main,
    }
