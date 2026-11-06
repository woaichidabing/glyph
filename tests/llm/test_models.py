"""可选主脑（agent 大脑）模型能力表测试。

这张表是"哪些型号能当大脑、各自支持什么"的唯一事实源：
- 第一批（2026-10-03，同一把千问 key、Responses 通道真机实测）：
  qwen3.8-max / deepseek-v4-pro-0813 / glm-5.2 / glm-5.3，流式 + 本地工具调用
  + 思考 low/medium/high + 平台 web_search 全部通过；
- 第二批（2026-10-06，项目所有者拍板接入）：deepseek-v4.1-flash / qwen3.8-flash
  / kimi-k3 / qwen3.8-27b；思考档按三档登记，平台联网按品牌保守给
  （flash / 27b 不挂平台联网、kimi-k3 联网），真机表现留待额度恢复后核对；
- 第三批（2026-10-08 真机补测）：qwen3.7-flash-2026-07-15，流式 + 本地工具
  调用通过、官方窗口 1M，但思考仅 low/medium（high 真机 400）、不挂平台联网；
  qwen3.5-flash 同期实测通过但型号低于 3.7，已移出主脑菜单（仍保留为批改子模型）；
- glm-5.1 / glm-5 在 Responses 通道报 "Agent capabilities are not enabled"，
  连工具都调不了，当不了大脑，绝不能放进选择菜单。
"""

from __future__ import annotations

from glyph.llm.models import (
    CONTEXT_MODE_MAX,
    CONTEXT_MODE_STANDARD,
    CONTEXT_WINDOW_MAX,
    CONTEXT_WINDOW_STANDARD,
    MAIN_MODELS,
    REASONING_LEVELS,
    MainModel,
    context_window_of,
    is_supported_main_model,
    main_model,
    network_capable,
    supports_reasoning,
)

# 可当大脑的全部型号：第一批四个 + 第二批四个（2026-10-06）+ 第三批一个满额 flash（2026-10-08）
EXPECTED_IDS = {
    "qwen3.8-max",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "glm-5.3",
    "deepseek-v4.1-flash",
    "qwen3.8-flash",
    "qwen3.7-flash-2026-07-15",
    "kimi-k3",
    "qwen3.8-27b",
}
# 平台联网能力：第一批四个 ＋ kimi-k3；各 flash 与 27b 不挂平台联网
WEBSEARCH_IDS = {
    "qwen3.8-max",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "glm-5.3",
    "kimi-k3",
}
NO_WEBSEARCH_IDS = {
    "deepseek-v4.1-flash",
    "qwen3.8-flash",
    "qwen3.7-flash-2026-07-15",
    "qwen3.8-27b",
}
# 第三批满额 flash 真机只接受 low/medium（high 经 Responses 通道 400），其余大脑三档齐全
TWO_LEVEL_IDS = {"qwen3.7-flash-2026-07-15"}


def test_lists_all_registered_brains():
    assert {m.id for m in MAIN_MODELS} == EXPECTED_IDS


def test_every_listed_brain_has_one_line_description():
    # 选择器在模型名下方渲染一行灰字描述（B7）：每个在表大脑都必须带非空 desc
    for m in MAIN_MODELS:
        assert m.desc.strip()


def test_every_listed_brain_has_verified_reasoning_levels():
    # 第三批满额 flash 真机只接受 low/medium（high 报 400），其余大脑思考三档齐全
    for m in MAIN_MODELS:
        assert m.context_window > 0
        if m.id in TWO_LEVEL_IDS:
            assert m.reasoning_levels == ("low", "medium")
            assert supports_reasoning(m.id, "high") is False
        else:
            assert m.reasoning_levels == REASONING_LEVELS == ("low", "medium", "high")


def test_websearch_follows_verified_brand_capability():
    for m in MAIN_MODELS:
        assert m.web_search is (m.id in WEBSEARCH_IDS)
    assert WEBSEARCH_IDS | NO_WEBSEARCH_IDS == EXPECTED_IDS
    assert WEBSEARCH_IDS.isdisjoint(NO_WEBSEARCH_IDS)


def test_network_capable_intersects_switch_with_model_capability():
    # 全局开关关掉：任何型号（含能联网 / 未知）都不联网
    assert network_capable("qwen3.8-max", enabled=False) is False
    assert network_capable("qwen3.8-flash", enabled=False) is False
    assert network_capable("nope-xyz", enabled=False) is False
    assert network_capable(None, enabled=False) is False
    # 开关打开：只有「在表且自身支持联网」的型号才联网
    assert network_capable("qwen3.8-max", enabled=True) is True
    assert network_capable("kimi-k3", enabled=True) is True
    # 型号在表但自身不挂平台联网：不联网
    assert network_capable("qwen3.8-flash", enabled=True) is False
    assert network_capable("deepseek-v4.1-flash", enabled=True) is False
    assert network_capable("qwen3.8-27b", enabled=True) is False
    # 未知型号 / None：安全回退不联网，绝不默认下发 web_search
    assert network_capable("nope-xyz", enabled=True) is False
    assert network_capable(None, enabled=True) is False


def test_labels_are_plain_model_names_without_marketing_prefix():
    # 用户要求菜单只显示纯模型名，不加"旗舰/深度推理"之类前缀
    for m in MAIN_MODELS:
        assert m.label == m.id


def test_glm51_and_glm5_are_not_offered_as_brains():
    # 实测这两个型号在 Responses 通道没有 agent 能力（不能工具调用），不得进大脑菜单
    assert main_model("glm-5.1") is None
    assert main_model("glm-5") is None
    assert is_supported_main_model("glm-5.1") is False


def test_lookup_known_and_unknown_id():
    m = main_model("glm-5.3")
    assert isinstance(m, MainModel)
    assert m.id == "glm-5.3"
    assert main_model("nope-xyz") is None
    assert is_supported_main_model("qwen3.8-max") is True


def test_supports_reasoning_checks_both_model_and_level():
    assert supports_reasoning("qwen3.8-max", "high") is True
    assert supports_reasoning("qwen3.8-max", "bogus") is False
    # 不在大脑表里的型号（无 agent 能力）谈不上思考档
    assert supports_reasoning("glm-5.1", "low") is False


def test_context_window_is_a_sane_positive_size():
    # 上下文环的分母：至少是 64k 量级，不能是 0 或缺失导致环除零 / 恒满
    assert context_window_of("qwen3.8-max") >= 64_000


def test_listed_brains_nominal_window_is_one_million():
    # 四个在表大脑的官方上下文窗口都是 1M（2026-09/10 厂商文档），标称登记为 1_000_000
    for m in MAIN_MODELS:
        assert m.context_window == 1_000_000


def test_standard_mode_caps_window_at_300k():
    assert context_window_of("qwen3.8-max", CONTEXT_MODE_STANDARD) == CONTEXT_WINDOW_STANDARD
    assert CONTEXT_WINDOW_STANDARD == 300_000


def test_max_mode_opens_window_to_one_million():
    assert context_window_of("qwen3.8-max", CONTEXT_MODE_MAX) == CONTEXT_WINDOW_MAX
    assert CONTEXT_WINDOW_MAX == 1_000_000


def test_default_single_arg_call_is_standard_300k():
    # loop 现状的单参调用必须向后兼容：缺省档位即标准档 30 万
    assert context_window_of("qwen3.8-max") == 300_000


def test_unknown_model_never_pretends_one_million_and_never_zero():
    # 未知 / 测试假模型按兜底 256k（2026-10-29 由 128k 上调）：绝不为假型号假装 1M，也不返回 0
    assert context_window_of("nope-xyz", CONTEXT_MODE_STANDARD) == 256_000
    assert context_window_of("nope-xyz", CONTEXT_MODE_MAX) == 256_000
    assert context_window_of(None) == 256_000
    assert context_window_of(None, CONTEXT_MODE_MAX) > 0


def test_invalid_mode_falls_back_to_standard_and_accepts_uppercase():
    assert context_window_of("qwen3.8-max", "ultra") == 300_000
    assert context_window_of("qwen3.8-max", "MAX") == 1_000_000
