"""配置读取测试（离线）。

重点是 key 选择优先级：机器上可能同时存在系统环境变量 DASHSCOPE_API_KEY
（额度受限的官方 key）和项目 .env 里的 GLYPH_API_KEY（千问平台 key）。
load_dotenv 刻意"不覆盖已存在环境变量"，因此用独立的 GLYPH_* 名字让 .env 的
key 能生效，且 GLYPH_* 优先、DASHSCOPE_* 回退。
"""

from glyph.config import (
    CONTEXT_MODE_MAX,
    CONTEXT_MODE_STANDARD,
    CONTEXT_WINDOW_MAX,
    CONTEXT_WINDOW_STANDARD,
    DEFAULT_CONTEXT_MODE,
    DEFAULT_GRADE_MODEL,
    DEFAULT_MAIN_FALLBACK_MODELS,
    DEFAULT_MODEL,
    DEFAULT_OCR_MAX_BYTES,
    DEFAULT_RECHECK_MAX_CONTEXT_CHARS,
    DEFAULT_RECHECK_MAX_ROUNDS,
    DEFAULT_RECHECK_MAX_TOTAL,
    DEFAULT_RECHECK_MIN_CONTEXT_WORDS,
    DEFAULT_SESSION_POOL_MAX,
    DEFAULT_SPEAKING_STYLE,
    DEFAULT_VISION_MODEL,
    SPEAKING_STYLES,
    load_settings,
)


def test_markr_api_key_takes_precedence(monkeypatch):
    monkeypatch.setenv("GLYPH_API_KEY", "markr-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")

    settings = load_settings()

    assert settings.api_key == "markr-key"


def test_falls_back_to_dashscope_key_when_markr_absent(monkeypatch):
    # 用空串模拟"未设置 GLYPH_API_KEY"，且阻止 .env 注入覆盖该名字
    monkeypatch.setenv("GLYPH_API_KEY", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dash-key")

    assert load_settings().api_key == "dash-key"


def test_markr_base_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("GLYPH_BASE_URL", "https://glyph.example/v1")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://dash.example/v1")

    assert load_settings().base_url == "https://glyph.example/v1"


def test_grade_model_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_GRADE_MODEL", "custom-grade-model")

    assert load_settings().grade_model == "custom-grade-model"


def test_main_and_grade_models_default_to_pro(monkeypatch):
    # 清空显式覆盖后，开箱即用主对话与批改都应是强模型 deepseek-v4-pro
    # （.env 也配置为 pro；本测试锁定"开箱即用是 pro"这一事实）
    monkeypatch.setenv("GLYPH_QWEN_MODEL", "")
    monkeypatch.setenv("GLYPH_GRADE_MODEL", "")

    settings = load_settings()

    assert settings.model == "deepseek-v4-pro"
    assert settings.grade_model == DEFAULT_GRADE_MODEL == DEFAULT_MODEL


def test_main_fallback_models_default_to_kimi() -> None:
    # 没显式配置时，主脑零产出撞额度的第一替补固定是 kimi-k3（同千问网关、满额、能联网）
    assert DEFAULT_MAIN_FALLBACK_MODELS == ("kimi-k3",)
    assert load_settings().main_fallback_models == ("kimi-k3",)


def test_main_fallback_models_overridable_as_csv(monkeypatch) -> None:
    monkeypatch.setenv("GLYPH_MAIN_FALLBACK_MODELS", "kimi-k3, qwen3.8-27b")

    assert load_settings().main_fallback_models == ("kimi-k3", "qwen3.8-27b")


def test_ocr_defaults(monkeypatch):
    """OCR 三件套的默认值：忠实型视觉模型 + 10MB 上限（见 DEFAULT_VISION_MODEL 注释）。"""
    monkeypatch.delenv("GLYPH_VISION_MODEL", raising=False)
    monkeypatch.delenv("GLYPH_OCR_MAX_BYTES", raising=False)

    settings = load_settings()

    assert settings.vision_model == DEFAULT_VISION_MODEL == "qwen3-vl-flash"
    assert settings.ocr_max_bytes == DEFAULT_OCR_MAX_BYTES == 10 * 1024 * 1024


def test_session_pool_max_default_and_overridable(monkeypatch):
    """会话池上限：默认 4，可用 GLYPH_SESSION_POOL_MAX 覆盖（并发模型的水位线）。"""
    monkeypatch.delenv("GLYPH_SESSION_POOL_MAX", raising=False)
    assert load_settings().session_pool_max == DEFAULT_SESSION_POOL_MAX == 4

    monkeypatch.setenv("GLYPH_SESSION_POOL_MAX", "2")
    assert load_settings().session_pool_max == 2


def test_recheck_defaults(monkeypatch):
    """复核三道闸门的默认值：单条 2 次 / 单篇 5 次 / 上下文 4096 字符。"""
    for name in (
        "GLYPH_RECHECK_MAX_ROUNDS",
        "GLYPH_RECHECK_MAX_TOTAL",
        "GLYPH_RECHECK_MAX_CONTEXT_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.recheck_max_rounds == DEFAULT_RECHECK_MAX_ROUNDS == 2
    assert settings.recheck_max_total == DEFAULT_RECHECK_MAX_TOTAL == 5
    assert settings.recheck_max_context_chars == DEFAULT_RECHECK_MAX_CONTEXT_CHARS == 4096


def test_recheck_min_context_words_default_and_overridable(monkeypatch):
    """句子不足 8 词就升段落（设计稿 §1.8 L5b），阈值必须在配置单点里可配。"""
    monkeypatch.delenv("GLYPH_RECHECK_MIN_CONTEXT_WORDS", raising=False)
    assert load_settings().recheck_min_context_words == DEFAULT_RECHECK_MIN_CONTEXT_WORDS == 8

    monkeypatch.setenv("GLYPH_RECHECK_MIN_CONTEXT_WORDS", "12")
    assert load_settings().recheck_min_context_words == 12


def test_vision_and_recheck_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_VISION_MODEL", "my-vl")
    monkeypatch.setenv("GLYPH_RECHECK_MAX_ROUNDS", "7")

    settings = load_settings()

    assert settings.vision_model == "my-vl"
    assert settings.recheck_max_rounds == 7


def test_memory_defaults(monkeypatch):
    """长期记忆六个旋钮的默认值（设计稿 §3.9）。"""
    for name in (
        "GLYPH_MEMORY_LINE_MAX_CHARS",
        "GLYPH_MEMORY_CATALOG_MAX_ENTRIES",
        "GLYPH_MEMORY_CATALOG_MAX_BYTES",
        "GLYPH_MEMORY_BODY_MAX_BYTES",
        "GLYPH_MEMORY_FACT_UNUSED_DAYS",
        "GLYPH_MEMORY_TITLE_MAX_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.memory_line_max_chars == 150
    assert settings.memory_catalog_max_entries == 50
    assert settings.memory_catalog_max_bytes == 32 * 1024
    assert settings.memory_body_max_bytes == 4 * 1024
    assert settings.memory_fact_unused_days == 30
    assert settings.memory_title_max_chars == 40


def test_memory_settings_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_MEMORY_CATALOG_MAX_ENTRIES", "20")
    monkeypatch.setenv("GLYPH_MEMORY_FACT_UNUSED_DAYS", "14")

    settings = load_settings()

    assert settings.memory_catalog_max_entries == 20
    assert settings.memory_fact_unused_days == 14


def test_reasoning_effort_defaults_to_medium(monkeypatch):
    """主脑思考档开箱即用为"中"（均衡档）；产品不设自动挡 / 关闭档。"""
    monkeypatch.delenv("GLYPH_REASONING_EFFORT", raising=False)

    assert load_settings().reasoning_effort == "medium"


def test_reasoning_effort_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_REASONING_EFFORT", "high")

    assert load_settings().reasoning_effort == "high"


def test_reasoning_effort_accepts_uppercase(monkeypatch):
    # 环境变量手滑写大写也应归一化，而不是当成非法值
    monkeypatch.setenv("GLYPH_REASONING_EFFORT", "LOW")

    assert load_settings().reasoning_effort == "low"


def test_reasoning_effort_invalid_value_falls_back_to_medium(monkeypatch):
    # 空串 / 拼错 / 已被产品砍掉的"关闭、自动"档，一律回退默认中档，
    # 绝不把一个模型不认的档位静默发出去
    for bad in ("", "ultra", "off", "auto", "关闭"):
        monkeypatch.setenv("GLYPH_REASONING_EFFORT", bad)
        assert load_settings().reasoning_effort == "medium"


def test_context_window_tier_constants():
    # 产品拍板的两档上限：标准 30 万 / Max 100 万，默认标准
    assert CONTEXT_WINDOW_STANDARD == 300_000
    assert CONTEXT_WINDOW_MAX == 1_000_000
    assert DEFAULT_CONTEXT_MODE == CONTEXT_MODE_STANDARD == "standard"
    assert CONTEXT_MODE_MAX == "max"


def test_context_mode_defaults_to_standard(monkeypatch):
    """开箱即用是标准档（30 万）：日常够用、更早压缩、更省更聚焦。"""
    monkeypatch.delenv("GLYPH_CONTEXT_MODE", raising=False)

    assert load_settings().context_mode == "standard"


def test_context_mode_overridable_to_max(monkeypatch):
    monkeypatch.setenv("GLYPH_CONTEXT_MODE", "max")

    assert load_settings().context_mode == "max"


def test_context_mode_accepts_uppercase(monkeypatch):
    monkeypatch.setenv("GLYPH_CONTEXT_MODE", "MAX")

    assert load_settings().context_mode == "max"


def test_context_mode_invalid_value_falls_back_to_standard(monkeypatch):
    # 空串 / 拼错 / 数字 / 中文等非法值一律回退标准档，绝不静默用一个不存在的档
    for bad in ("", "ultra", "off", "auto", "300k", "关闭"):
        monkeypatch.setenv("GLYPH_CONTEXT_MODE", bad)
        assert load_settings().context_mode == "standard"


def test_compaction_decision_defaults(monkeypatch):
    """决策层开箱默认值（方案 §6.3）：开关开、L2 50%、L3 预留 9 万、硬阻断预留 1.2 万、
    字节换算 3.5 字节/token × 1.15 安全边际。"""
    for name in (
        "GLYPH_AUTO_COMPACT",
        "GLYPH_COMPACT_PCT_L2",
        "GLYPH_COMPACT_RESERVE_COMPACT",
        "GLYPH_COMPACT_RESERVE_HARD",
        "GLYPH_BYTES_PER_TOKEN",
        "GLYPH_ESTIMATE_SAFETY",
    ):
        monkeypatch.delenv(name, raising=False)

    s = load_settings()
    assert s.auto_compact is True
    assert s.compact_pct_l2 == 0.50
    assert s.compact_reserve_compact == 90_000
    assert s.compact_reserve_hard == 12_000
    assert s.bytes_per_token == 3.5
    assert s.estimate_safety == 1.15


def test_auto_compact_switch_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("GLYPH_AUTO_COMPACT", "false")

    assert load_settings().auto_compact is False


def test_compaction_thresholds_overridable(monkeypatch):
    """阈值集中在 config 单点，校准（方案 §18）时靠环境变量即可调，不必改代码。"""
    monkeypatch.setenv("GLYPH_COMPACT_PCT_L2", "0.66")
    monkeypatch.setenv("GLYPH_COMPACT_RESERVE_COMPACT", "120000")
    monkeypatch.setenv("GLYPH_COMPACT_RESERVE_HARD", "20000")
    monkeypatch.setenv("GLYPH_BYTES_PER_TOKEN", "3.0")
    monkeypatch.setenv("GLYPH_ESTIMATE_SAFETY", "1.2")

    s = load_settings()
    assert s.compact_pct_l2 == 0.66
    assert s.compact_reserve_compact == 120_000
    assert s.compact_reserve_hard == 20_000
    assert s.bytes_per_token == 3.0
    assert s.estimate_safety == 1.2


def test_l2_packing_defaults(monkeypatch):
    """L2 装箱开箱默认值（方案 §8.2/§8.3，v2.1 #63）：最近 5 篇逐条锚点、提货单预算 4%、
    静默 3 个往返关闭任务、最近 3 轮保护窗口。"""
    for name in (
        "GLYPH_ANCHOR_DETAIL_RECENT",
        "GLYPH_ANCHOR_BUDGET_RATIO",
        "GLYPH_TASK_IDLE_TURNS",
        "GLYPH_FOLD_KEEP_RECENT",
        "GLYPH_ARTIFACT_RESULT_KEEP_RECENT",
    ):
        monkeypatch.delenv(name, raising=False)

    s = load_settings()
    assert s.anchor_detail_recent == 5
    assert s.anchor_budget_ratio == 0.04
    assert s.task_idle_turns == 3
    assert s.fold_keep_recent == 3
    assert s.artifact_result_keep_recent == 3


def test_l2_packing_thresholds_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_ANCHOR_DETAIL_RECENT", "8")
    monkeypatch.setenv("GLYPH_ANCHOR_BUDGET_RATIO", "0.05")
    monkeypatch.setenv("GLYPH_TASK_IDLE_TURNS", "4")
    monkeypatch.setenv("GLYPH_FOLD_KEEP_RECENT", "6")
    monkeypatch.setenv("GLYPH_ARTIFACT_RESULT_KEEP_RECENT", "5")

    s = load_settings()
    assert s.anchor_detail_recent == 8
    assert s.anchor_budget_ratio == 0.05
    assert s.task_idle_turns == 4
    assert s.fold_keep_recent == 6
    assert s.artifact_result_keep_recent == 5


def test_fold_keep_recent_below_floor_falls_back_to_default(monkeypatch):
    """C10 硬校验：最近保护窗口不得小于 3（否则正在裁决 / 取回讲解的篇目会被过早装箱）。
    非法值回退默认 3，绝不让一条配置把保护窗口打穿（压缩铁律：宁晚装、不装早）。"""
    monkeypatch.setenv("GLYPH_FOLD_KEEP_RECENT", "1")

    assert load_settings().fold_keep_recent == 3


def test_artifact_result_keep_recent_below_floor_falls_back(monkeypatch):
    """§8.4 B3：取回结果完整保留窗口也不得小于 3（保住"取回来正在讲解"），非法回退默认。"""
    monkeypatch.setenv("GLYPH_ARTIFACT_RESULT_KEEP_RECENT", "2")

    assert load_settings().artifact_result_keep_recent == 3


# ---------- L3 摘要压缩（v2.1 #65，方案 §9） ----------


def test_l3_compaction_defaults(monkeypatch):
    """L3 开箱默认：写手 qwen3.7-flash（标称 1M 窗口）、动态 tail 目标 60%、
    写手材料预算 60%、摘要输出预留 8k、tail 最少保留 2 个完整往返。"""
    for name in (
        "GLYPH_COMPACT_MODEL",
        "GLYPH_COMPACT_MODEL_WINDOW",
        "GLYPH_COMPACT_TARGET_RATIO",
        "GLYPH_COMPACT_MATERIAL_RATIO",
        "GLYPH_COMPACT_OUTPUT_RESERVE",
        "GLYPH_L3_TAIL_MIN_TURNS",
    ):
        monkeypatch.delenv(name, raising=False)

    s = load_settings()
    assert s.compact_model == "qwen3.7-flash"
    assert s.compact_model_window == 1_000_000
    assert s.compact_target_ratio == 0.60
    assert s.compact_material_ratio == 0.60
    assert s.compact_output_reserve == 8_000
    assert s.l3_tail_min_turns == 2


def test_l3_compaction_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_COMPACT_MODEL", "qwen3.8-flash")
    monkeypatch.setenv("GLYPH_COMPACT_MODEL_WINDOW", "256000")
    monkeypatch.setenv("GLYPH_COMPACT_TARGET_RATIO", "0.5")
    monkeypatch.setenv("GLYPH_COMPACT_MATERIAL_RATIO", "0.7")
    monkeypatch.setenv("GLYPH_COMPACT_OUTPUT_RESERVE", "12000")
    monkeypatch.setenv("GLYPH_L3_TAIL_MIN_TURNS", "3")

    s = load_settings()
    assert s.compact_model == "qwen3.8-flash"
    assert s.compact_model_window == 256_000
    assert s.compact_target_ratio == 0.5
    assert s.compact_material_ratio == 0.7
    assert s.compact_output_reserve == 12_000
    assert s.l3_tail_min_turns == 3


def test_l3_tail_min_turns_below_floor_falls_back(monkeypatch):
    """tail 至少保住 1 个完整往返（切点必须在 user 前、不能切空），配成 0/负数回退默认 2。"""
    monkeypatch.setenv("GLYPH_L3_TAIL_MIN_TURNS", "0")

    assert load_settings().l3_tail_min_turns == 2


# ---------- 说话风格（speaking_style：natural/witty/calm，默认 natural）----------


def test_speaking_style_choices_are_the_three_locked_tiers():
    """锁定三档（用户拍板，放弃第四档）：自然 / 风趣 / 冷静。"""
    assert SPEAKING_STYLES == ("natural", "witty", "calm")
    assert DEFAULT_SPEAKING_STYLE == "natural"


def test_speaking_style_defaults_to_natural(monkeypatch):
    """开箱即用是自然档；它决定系统提示词的口吻段（设置项归「助手」分类）。"""
    monkeypatch.delenv("GLYPH_SPEAKING_STYLE", raising=False)

    assert load_settings().speaking_style == "natural"


def test_speaking_style_overridable(monkeypatch):
    monkeypatch.setenv("GLYPH_SPEAKING_STYLE", "witty")

    assert load_settings().speaking_style == "witty"


def test_speaking_style_accepts_uppercase(monkeypatch):
    # 环境变量手滑写大写也应归一化，而不是当成非法值
    monkeypatch.setenv("GLYPH_SPEAKING_STYLE", "CALM")

    assert load_settings().speaking_style == "calm"


def test_speaking_style_invalid_value_falls_back_to_natural(monkeypatch):
    # 空串 / 拼错 / 中文等非法值一律回退自然档，和思考档 / 上下文档同一套容错
    for bad in ("", "funny", "严肃", "auto"):
        monkeypatch.setenv("GLYPH_SPEAKING_STYLE", bad)
        assert load_settings().speaking_style == "natural"
