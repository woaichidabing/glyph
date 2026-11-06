"""可选主脑（agent 大脑）模型能力表 —— 实测驱动的单一事实源。

只登记"经千问 Responses 通道实测能当 agent 大脑"的型号：必须**同时**支持流式与
本地 function 工具调用（这是主循环能编排工具的硬门槛）。思考档（reasoning.effort）
与平台联网 web_search 按真机实测勾选。

批改 / 看图 / 润色等**子模型不在此表**：它们不是大脑候选（本表只回答"能不能编排
工具"），但用户可在设置页「高级」里按能力指定（``provider.role_models``，两档通用，
未指定走自动适配），运行时注入点是 ``settings.grade_model / vision_model``（R1 单点）
—— 2026-10-13 起"用户不可见不可选"的旧口径作废。

实测记录（2026-10-03，同一把千问 key、``/compatible-mode/v1`` 的 Responses 通道；
探针为一次性脚本，结论固化于此表）：
- qwen3.8-max / deepseek-v4-pro-0813 / glm-5.2 / glm-5.3：流式 ✓、本地工具调用 ✓、
  reasoning.effort=low/medium/high 均接受（qwen3.8-max 同一道推理题 high 的
  reasoning_tokens 489 明显高于 low 的 206，档位真实生效）、web_search ✓；
- glm-5.1 / glm-5：400 ``Agent capabilities are not enabled for the current model``
  —— 连工具都调不了，当不了大脑（不是"不能思考"那么简单）；
- glm-5.3-flash：Responses 通道报 Unsupported model；glm-5.2-fast-preview：preview
  且免费额度耗尽，暂不登记。

同一个千问 endpoint + 同一把 key 即可调到 GLM（列表里有 glm-5.x 与 ZHIPU/GLM-*），
无需另接智谱 endpoint / key。

第二批（2026-10-06，项目所有者拍板接入；能力位按品牌保守登记，真机表现待额度恢复后核对）：
- deepseek-v4.1-flash / qwen3.8-flash：轻量 flash，思考三档、**不挂平台 web_search**；
- kimi-k3：思考三档、平台 web_search；
- qwen3.8-27b：思考三档、**不挂平台 web_search**。

第三批（2026-10-08，同一把 key、Responses 通道真机补测；动因是旗舰额度普遍耗尽、
开箱默认撞 403，需把当前满额、实测能当大脑的型号补进菜单）：
- qwen3.7-flash-2026-07-15：流式 ✓、本地 function 工具调用 ✓（正确发出
  grade_text(text)、stop=tool_calls，1.5–2.8s）；官方上下文窗口 1M
  （见 scripts/probe_compaction_summarizer.py 的窗口核查）；
- 思考档：只接受 low/medium，high 经 Responses 通道直接 400 InvalidParameter，
  故如实登记两档（不是三档）；
- 平台 web_search 本轮未测，按 flash 家族保守不挂（False），联网让给旗舰。
- qwen3.5-flash 同期真机同样通过（流式 + 本地工具、窗口 1M、思考仅 low/medium），
  但型号低于 3.7，2026-10 从主脑菜单移除；它仍作为批改 / 润色子模型保留
  （settings.grade_model，用户不可见），dated 快照 qwen3.5-flash-2026-02-23 不登记。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import (
    CONTEXT_MODE_MAX,
    CONTEXT_MODE_STANDARD,
    CONTEXT_WINDOW_MAX,
    CONTEXT_WINDOW_STANDARD,
    DEFAULT_CONTEXT_MODE,
    REASONING_HIGH,
    REASONING_LEVELS,
    REASONING_LOW,
    REASONING_MEDIUM,
    normalize_context_mode,
)

# 思考档常量（low/medium/high）的单点定义在 config.py，这里 re-export，
# 让"模型能力"相关的调用方可以只从 glyph.llm.models 取名。
# 产品刻意不做"自动"档与"关闭"档（用户拍板）：只有低 / 中 / 高三档。

__all__ = [
    "REASONING_LOW",
    "REASONING_MEDIUM",
    "REASONING_HIGH",
    "REASONING_LEVELS",
    "CONTEXT_MODE_STANDARD",
    "CONTEXT_MODE_MAX",
    "CONTEXT_WINDOW_STANDARD",
    "CONTEXT_WINDOW_MAX",
    "DEFAULT_CONTEXT_MODE",
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "normalize_context_mode",
    "MainModel",
    "MAIN_MODELS",
    "DEFAULT_MAIN_MODEL_ID",
    "main_model",
    "is_supported_main_model",
    "network_capable",
    "reasoning_levels_for",
    "supports_reasoning",
    "context_window_of",
]

# 上下文窗口（token）：上下文用量环的分母。Responses 每轮返回的 usage.input_tokens
# 就是"本轮实际发送的全部上下文"，正好当分子。窗口分两层概念：
# - 型号「标称窗口」（MainModel.context_window）：模型官方能装多少，四个在表大脑都是 1M；
# - 产品「档位上限」（CONTEXT_WINDOW_STANDARD/MAX）：用户选标准 30 万还是 Max 100 万。
# 实际分母 = min(标称, 档位上限)：标准档刻意在 30 万就提醒 / 压缩（更省、更聚焦），
# Max 档才放到模型真实的 1M；标准 30 万是产品取舍，不是模型只能装 30 万。
MODEL_CONTEXT_WINDOW = 1_000_000
# 未知型号 / 测试假模型的保守兜底标称：不假装它有大窗口，也保证环永不除零。
# 2026-10-29 由 128K 提到 256K（产品所有者定：自定义模型由用户自己填，没填就给够用的兜底）
DEFAULT_CONTEXT_WINDOW = 256_000
# 用户没填「最大输出」时的兜底（同日定 32K）
DEFAULT_MAX_OUTPUT_TOKENS = 32_768


@dataclass(frozen=True)
class MainModel:
    """一个可当大脑的型号及其能力（显示名 label 即纯模型 id，不加营销前缀）。"""

    id: str
    label: str
    # 支持的思考档；空元组表示该型号不支持思考（前端"推理强度"行整行变灰）。
    reasoning_levels: tuple[str, ...] = REASONING_LEVELS
    web_search: bool = True
    context_window: int = MODEL_CONTEXT_WINDOW
    # 单次最多能吐多少 token；0 = 没填（装配层按 DEFAULT_MAX_OUTPUT_TOKENS 兜底）
    max_output: int = 0
    # 模型名下方那一行灰字人话描述（能力 / 快慢 / 能否联网）；选择器直接渲染。
    desc: str = ""


# 菜单顺序即此元组顺序（第一项是选择器默认选中）。
MAIN_MODELS: tuple[MainModel, ...] = (
    MainModel(
        id="qwen3.8-max",
        label="qwen3.8-max",
        desc="能力最全的旗舰，思考深、能联网；偏慢、更耗额度",
    ),
    MainModel(
        id="deepseek-v4-pro-0813",
        label="deepseek-v4-pro-0813",
        desc="旗舰推理，能联网啃难题；偏慢、更耗额度",
    ),
    MainModel(
        id="glm-5.2",
        label="glm-5.2",
        desc="旗舰模型，能思考、能联网；偏慢、更耗额度",
    ),
    MainModel(
        id="glm-5.3",
        label="glm-5.3",
        desc="旗舰模型，能思考、能联网；偏慢、更耗额度",
    ),
    # 第二批（2026-10-06 接入）：flash 与 27b 不挂平台联网，kimi-k3 联网
    MainModel(
        id="deepseek-v4.1-flash",
        label="deepseek-v4.1-flash",
        web_search=False,
        desc="轻快省额度，日常对话和批改都稳；不能联网",
    ),
    MainModel(
        id="qwen3.8-flash",
        label="qwen3.8-flash",
        web_search=False,
        desc="轻快省额度、响应快，适合日常；不能联网",
    ),
    # 第三批（2026-10-08 真机补测，见模块 docstring）：当前满额的 qwen3.7 flash；
    # 思考只支持 low/medium（high 真机报 400），平台联网未测、保守不挂。
    MainModel(
        id="qwen3.7-flash-2026-07-15",
        label="qwen3.7-flash-2026-07-15",
        web_search=False,
        reasoning_levels=("low", "medium"),
        desc="轻快省额度；只支持低/中思考，不能联网",
    ),
    MainModel(
        id="kimi-k3",
        label="kimi-k3",
        desc="长文本强、能联网，适合读长材料；更耗额度",
    ),
    MainModel(
        id="qwen3.8-27b",
        label="qwen3.8-27b",
        web_search=False,
        desc="思考三档、表现均衡；不能联网",
    ),
)

# 选择器首次启动（本地无记忆）时默认选中的大脑。**不固定为菜单第一项旗舰**：2026-10
# 旗舰（qwen3.8-max / deepseek-v4-pro 系列）免费额度普遍耗尽（2026-11-15 到期恢复），
# 默认若指向旗舰，新用户开箱第一轮就撞 403。故默认指向"当前实测稳定且有免费额度"的
# deepseek-v4.1-flash —— 它已端到端真机跑通主脑（流式 / 本地工具编排 / 思考三档都稳）。
# 旗舰仍按能力排在菜单顶部，额度恢复后用户可手动切回；额度是临时的，菜单顺序（按能力）
# 不随临时额度重排，只改这个默认常量。
DEFAULT_MAIN_MODEL_ID = "deepseek-v4.1-flash"

_BY_ID: dict[str, MainModel] = {m.id: m for m in MAIN_MODELS}
# 默认大脑必须真的在菜单里：常量拼写错误会在导入时立刻炸，而不是等到前端拿到一个
# 菜单中不存在的 default 才暴露。
assert DEFAULT_MAIN_MODEL_ID in _BY_ID, "DEFAULT_MAIN_MODEL_ID 必须是 MAIN_MODELS 里的 id"


def main_model(model_id: str) -> MainModel | None:
    """按 API id 取大脑能力；不在表（不能当大脑 / 未知）返回 None。"""
    return _BY_ID.get(model_id)


def is_supported_main_model(model_id: str) -> bool:
    """该 id 是否为登记在册、可当大脑的型号。"""
    return model_id in _BY_ID


def network_capable(model_id: str | None, enabled: bool) -> bool:
    """全局联网开关与「所选型号自身能力」求交：两者都满足才真正联网。

    - 开关关：一律不联网（型号再强也不下发 web_search）；
    - 型号在表：返回该型号自身的 web_search 能力（flash / 27b 为 False）；
    - 未知型号 / None：安全回退不联网，绝不替陌生型号默认下发 web_search。
    """
    if not enabled:
        return False
    m = _BY_ID.get(model_id)
    return bool(m and m.web_search)


def reasoning_levels_for(model_id: str) -> tuple[str, ...]:
    """该型号支持的思考档；不在表返回空元组（调用方据此把"推理强度"行置灰）。"""
    m = _BY_ID.get(model_id)
    return m.reasoning_levels if m is not None else ()


def supports_reasoning(model_id: str, level: str) -> bool:
    """该型号是否支持某个具体思考档（型号要在大脑表、且档位在其支持列表里）。"""
    return level in reasoning_levels_for(model_id)


def context_window_of(model_id: str | None, mode: str = DEFAULT_CONTEXT_MODE) -> int:
    """该型号在某档位下的上下文窗口（环的分母 / 压缩阈值上限）；取「型号标称窗口」与
    「档位上限」的较小者。

    - 在表大脑标称 1M：标准档给 30 万、Max 档给 100 万；
    - 未知型号 / None（测试假模型）标称保守 128k，两档都不超过它，绝不假装它有 1M；
    - mode 非法（大小写容错后仍不认识）回退标准档；返回值恒 > 0，用量环永不除零。
    缺省 mode 为标准档，兼容既有单参调用。
    """
    nominal = _BY_ID[model_id].context_window if model_id in _BY_ID else DEFAULT_CONTEXT_WINDOW
    tier = normalize_context_mode(mode)
    cap = CONTEXT_WINDOW_MAX if tier == CONTEXT_MODE_MAX else CONTEXT_WINDOW_STANDARD
    return max(1, min(nominal, cap))
