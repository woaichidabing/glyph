"""自定义上游模型的能力推断测试（标准 GET /models 只回 id，能力靠模型名推断 + 保守兜底）。"""

from __future__ import annotations

from glyph.llm.models import REASONING_LEVELS
from glyph.provider.capabilities import (
    DEFAULT_INFERRED_CONTEXT,
    capabilities_for,
    capabilities_payload,
    infer_brand,
    infer_capabilities,
)

# ---------- 品牌（logo 族） ----------


def test_brand_recognizes_major_families() -> None:
    cases = {
        "gpt-4o": "openai",
        "gpt-5-mini": "openai",
        "o3-mini": "openai",
        "claude-3-5-sonnet-20241022": "anthropic",
        "claude-4-opus": "anthropic",
        "gemini-2.5-pro": "google",
        "gemini-2.0-flash": "google",
        "deepseek-chat": "deepseek",
        "vanchin/deepseek-v4.1-flash": "deepseek",
        "glm-4v": "zhipu",
        "ZHIPU/GLM-5.3-FlashX": "zhipu",
        "qwen3.8-27b": "qwen",
        "qwq-32b": "qwen",
        "kimi/kimi-k2.8-preview": "kimi",
        "grok-3": "xai",
        "stepfun/step-5-preview": "stepfun",
        "llama-3.3-70b-instruct": "meta",
        "mixtral-8x7b-instruct": "mistral",
        "MiniMax/MiniMax-M3": "minimax",
        "qvq-max": "qwen",
    }
    for model_id, expected in cases.items():
        assert infer_brand(model_id) == expected, model_id


def test_brand_unknown_is_generic() -> None:
    assert infer_brand("some-random-internal-model") == "generic"
    assert infer_brand("acme-llm-7b") == "generic"


# ---------- 能不能当大脑（对话 + 工具调用） ----------


def test_chat_models_can_be_main() -> None:
    for model_id in ["gpt-4o", "claude-3-5-sonnet", "qwen3-235b-a22b", "deepseek-chat"]:
        cap = infer_capabilities(model_id)
        assert cap.can_be_main is True, model_id
        assert cap.supports_tools is True, model_id


def test_non_chat_models_cannot_be_main() -> None:
    non_chat = [
        "qwen-image-2.1-pro",
        "dall-e-3",
        "sora-2",
        "tts-1",
        "qwen3.8-omni-flash-realtime",
        "qwen-audio-3.1-realtime-plus",
        "whisper-1",
        "text-embedding-3-small",
        "bge-m3",
        "bge-large-zh-v1.5",
        "qwen-mt-uni",
        "qwen3.8-livetranslate-flash-realtime",
        "jina-reranker-v2",
        "qwen-omni-turbo",
        "test-sre-gpu-auto-handle",
    ]
    for model_id in non_chat:
        cap = infer_capabilities(model_id)
        assert cap.can_be_main is False, model_id
        assert cap.supports_tools is False, model_id


# ---------- 多模态看图（宁漏勿滥） ----------


def test_vision_models_detected() -> None:
    vision = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "claude-3-5-sonnet",
        "claude-3-haiku",
        "claude-4-sonnet",
        "gemini-2.0-flash",
        "qwen3-vl-plus",
        "qwen2.5-vl-72b",
        "glm-4v-plus",
        "qwen3.8-omni-flash",
        "llama-3.2-90b-vision-instruct",
        "internvl2-26b",
        "qvq-plus",
    ]
    for model_id in vision:
        assert infer_capabilities(model_id).supports_vision is True, model_id


def test_plain_text_models_not_marked_vision() -> None:
    text = [
        "gpt-3.5-turbo",
        "deepseek-chat",
        "qwen3.8-27b",
        "gpt-4-turbo-preview",  # 早期 preview 不稳，宁漏
        "glm-4.6",
        "llama-3.3-70b-instruct",
        "some-random-model",
    ]
    for model_id in text:
        assert infer_capabilities(model_id).supports_vision is False, model_id


# ---------- 思考档（只给高置信、兼容层认 reasoning 的家族） ----------


def test_reasoning_enabled_only_for_high_confidence_families() -> None:
    with_levels = ["gpt-5", "gpt-5-pro", "o3-mini", "o1", "o4-mini", "qwen3-235b", "qwq-32b"]
    for model_id in with_levels:
        assert infer_capabilities(model_id).reasoning_levels == REASONING_LEVELS, model_id

    without_levels = [
        "gpt-4o",  # 4o 无 effort 档
        "claude-3-7-sonnet",  # 第三方代理 effort 参数不统一，保守不发
        "gemini-2.5-pro",
        "deepseek-chat",
        "deepseek-r1",  # reasoner 自动思考、不吃 effort 档
        "glm-4.6",
        "some-random-model",
    ]
    for model_id in without_levels:
        assert infer_capabilities(model_id).reasoning_levels == (), model_id


# ---------- 上下文窗口（宁小勿大） ----------


def test_context_window_conservative_defaults() -> None:
    assert infer_capabilities("some-random-model").context_window == DEFAULT_INFERRED_CONTEXT
    assert infer_capabilities("gpt-4o").context_window == 256_000
    assert infer_capabilities("deepseek-chat").context_window == 256_000
    assert infer_capabilities("claude-3-5-sonnet").context_window == 200_000
    assert infer_capabilities("gemini-2.5-pro").context_window == 1_000_000
    # 非内置表的普通 qwen（2.x）按兜底 256k，不盲目给 1M
    assert infer_capabilities("qwen2.5-72b-instruct").context_window == 256_000


# ---------- 内置实测表优先 ----------


def test_capabilities_for_prefers_builtin_table() -> None:
    # qwen3.8-27b 在内置表：标称 1M、思考三档，以实测表为准
    cap = capabilities_for("qwen3.8-27b")
    assert cap.context_window == 1_000_000
    assert cap.reasoning_levels == REASONING_LEVELS
    assert cap.can_be_main is True
    assert cap.brand == "qwen"
    assert cap.supports_vision is False  # 内置文本主脑不看图（看图走独立 vision 子模型）

    # qwen3.7-flash 内置表只给 low/medium，推断不得覆盖成三档
    cap2 = capabilities_for("qwen3.7-flash-2026-07-15")
    assert cap2.reasoning_levels == ("low", "medium")


def test_capabilities_for_falls_back_to_inference_for_unknown() -> None:
    cap = capabilities_for("internal-chat-model-v2")
    assert cap.can_be_main is True
    assert cap.supports_tools is True
    assert cap.brand == "generic"
    assert cap.supports_vision is False
    assert cap.reasoning_levels == ()
    assert cap.context_window == DEFAULT_INFERRED_CONTEXT


def test_payload_shape() -> None:
    payload = capabilities_payload("gpt-4o")
    assert payload == {
        "id": "gpt-4o",
        "brand": "openai",
        "context_window": 256_000,
        "reasoning_levels": [],
        "supports_vision": True,
        "can_be_main": True,
    }
