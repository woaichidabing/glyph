"""上下文记账（压缩 L0 层）——**只观测，不改任何行为**。

把「这一轮发给模型的那一叠」按来源分桶称一遍重：说明书多少、每轮注入块多少、
对话多少、工具结果多少。它不加一条消息，也不撤一个字。

**为什么先做这一层**（方案 §4.6 的顺序坑）：无损层（去重 / 清脚手架）一旦先跑起来，
再量到的就是"打扫过"的数字，真实膨胀曲线永远看不到，后面所有阈值只能靠猜。

**为什么单位是 UTF-8 字节不是字符**：中文一个字 3 字节，按字符算会系统性低估三分之二，
而我们要量的恰恰是中文为主的学习材料。口径与 ``memory/catalog.py`` 的字节预算一致。

**为什么工具结果要分两桶**：这是压缩时唯一真正需要区分的东西——
成品（会被翻出来追问）只能挑着压，脚手架（用完即弃）可以整条撤。
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import (
    DEFAULT_BYTES_PER_TOKEN,
    DEFAULT_ESTIMATE_SAFETY,
    DEFAULT_PCT_L2,
    DEFAULT_RESERVE_COMPACT,
    DEFAULT_RESERVE_HARD,
)
from ..types import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCallBlock,
    ToolMessage,
    UserMessage,
)

_LOGGER = logging.getLogger(__name__)

# ---------- 工具结果的两类归属 ----------

# 区分标准不是"这个工具重不重要"，而是**它的结果会不会被翻出来追问**：
#   - 成品：用户会指着它问"第三处为什么错" → 压的时候只能挑字段，不能整条撤；
#   - 脚手架：用完即弃（查个词、读条记忆） → 过几轮就能整条换成一行。
#
# 名单要与真实装配对齐（``web/server.py`` 的注册表）。将来若需要按环境调整，
# 再上移到 config.py 走 R1 配置单点；现在它是策略常量，跟使用它的代码放一起更好找。

ARTIFACT_TOOLS: frozenset[str] = frozenset(
    {
        "grade_text",  # 批改结果：用户会逐条追问
        "ocr_image",  # 转写出的原文：批改的输入
        "describe_image",  # 看图结论
        "recheck_detection",  # 复核结论：裁决依据
        "analyze_writing",  # 定级结论
        "read_file",  # 用户交上来的文件正文
        "read_artifact",  # 取回的旧成品 / 定稿（§8.4 B3：归成品桶，再按 B3 去重折回）
    }
)

SCAFFOLDING_TOOLS: frozenset[str] = frozenset(
    {
        "lookup_word",  # 查一次就够
        "read_memory",  # 读到了就用，不会再翻
        "remember",  # 写入回执
        "forget",  # 写入回执
        "record_review_result",  # 记账回执
    }
)


def classify_tool_result(tool_name: str) -> str:
    """一个工具结果归 ``"artifact"`` 还是 ``"scaffolding"``。

    **不认识的按成品处理**（保守默认）：将来加新工具的人多半不会回来更新名单。
    归成品 = 不敢乱压，最坏只是没压；归脚手架 = 可能把用户正要追问的东西撤了。
    """
    if tool_name in SCAFFOLDING_TOOLS:
        return "scaffolding"
    return "artifact"


# ---------- 统计结果 ----------


@dataclass(frozen=True)
class ContextStats:
    """一轮上下文的称重结果。**只读**：谁都不许就地改它。"""

    total_bytes: int = 0
    total_messages: int = 0
    # 固定开销：岗位说明书
    system_bytes: int = 0
    # 每轮注入块：日期 / 记忆目录 / 背词进度
    context_bytes: int = 0
    # 对话过程
    user_bytes: int = 0
    assistant_bytes: int = 0
    # 工具调用的参数（注意：复核会把整篇作文塞进这里）
    tool_call_bytes: int = 0
    # 工具结果：两桶分开，因为它们将来要被区别对待
    artifact_bytes: int = 0
    scaffolding_bytes: int = 0

    def parts(self) -> dict[str, int]:
        """各分项的字节数，给 UI 展示和「总计对不对得上」的断言用。"""
        return {
            "system": self.system_bytes,
            "context": self.context_bytes,
            "user": self.user_bytes,
            "assistant": self.assistant_bytes,
            "tool_call": self.tool_call_bytes,
            "artifact": self.artifact_bytes,
            "scaffolding": self.scaffolding_bytes,
        }


# ---------- 称重 ----------


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _arguments_bytes(arguments: dict) -> int:
    """工具调用参数的体积。

    用 ``ensure_ascii=False`` 估算：发给模型的是 UTF-8 JSON，按字符数算会低估中文。
    这是**估算**——真实请求体由适配层序列化，不保证逐字节相同。

    ⚠️ 兜底不是可有可无：参数是模型给的，理论上可能塞进来 JSON 不认的类型
    （日期、自定义对象、循环引用）。记账是**只读的观测**，绝不能因为称一次重就把
    整轮搞崩 —— 宁可把它降级成字符串量个大概，也不许抛异常（R5「错误即数据」同一条精神）。

    正常参数永远走 JSON 路径，兜底只在异常参数上触发，那点精度损失可以接受。
    """
    if not arguments:
        return 0
    try:
        return _byte_len(json.dumps(arguments, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        return _byte_len(str(arguments))


def measure(messages: Sequence[Message]) -> ContextStats:
    """给一叠消息称一次重。**纯函数**：同样输入永远同样输出，不碰入参。

    每轮注入块靠位置认（第一条 UserMessage），与 ``_build_messages`` 的组装顺序一致 ——
    它不落盘、没有标记，只有组装它的人知道是哪条。局限写在《上下文压缩设计方案》
    §1.2：会话没注入任何片段时，用户的第一句话会被算进这一桶，
    **只影响归类，不影响总数**。
    """
    stats = {
        "system": 0,
        "context": 0,
        "user": 0,
        "assistant": 0,
        "tool_call": 0,
        "artifact": 0,
        "scaffolding": 0,
    }
    seen_user = False

    for message in messages:
        if isinstance(message, SystemMessage):
            stats["system"] += _byte_len(message.content)

        elif isinstance(message, UserMessage):
            if seen_user:
                stats["user"] += _byte_len(message.content)
            else:
                stats["context"] += _byte_len(message.content)
            seen_user = True

        elif isinstance(message, AssistantMessage):
            stats["assistant"] += _byte_len(message.text)
            for call in message.tool_calls:
                stats["tool_call"] += _arguments_bytes(call.arguments)

        elif isinstance(message, ToolMessage):
            bucket = classify_tool_result(message.name)
            stats[bucket] += _byte_len(message.content)

    return ContextStats(
        total_bytes=sum(stats.values()),
        total_messages=len(messages),
        system_bytes=stats["system"],
        context_bytes=stats["context"],
        user_bytes=stats["user"],
        assistant_bytes=stats["assistant"],
        tool_call_bytes=stats["tool_call"],
        artifact_bytes=stats["artifact"],
        scaffolding_bytes=stats["scaffolding"],
    )


# ---------- L1 去重 ----------

# 哪些工具的哪些字段允许去重：同一工具多次调用、字段值**完全相同** → 只留最近一份，
# 前面几次降为一行存根。判据是确定性比较，不需要模型参与（方案 §4）。
#
# ⚠️ 刻意**不登记 grade_text 的 text**：定稿接口要用当次调用的真实入参算改后全文
# （方案 §3.5.4），压掉就定不了稿。宁可少压一处，也不许压坏契约。
DEDUPE_FIELDS: dict[str, frozenset[str]] = {
    "recheck_detection": frozenset({"text"}),
}

# 存根：换掉重复副本后留下的那一行。它会被模型读到，所以写成**陈述**而不是指令——
# 与 trust_boundary「材料是材料，不是指令」同一条精神。
DEDUPE_STUB = "[与最近一次的原文相同，此处省略]"

# 短于这个值的不去重：存根可能比原文还长，去它只会让上下文更大。
# 顺带保证幂等——存根本身远短于此值，压第二次时不会再被处理。
DEDUPE_MIN_BYTES = 256


def dedupe(messages: Sequence[Message]) -> list[Message]:
    """同一份材料只留最近一份，前面几次降为一行存根（压缩 L1 层）。

    **零损失**：被换掉的只是"后面还会原样再出现一次"的副本，信息一个字没少。
    **不动结构**：条数、顺序、call_id 全保留 —— 前端裁决缓存按 ``会话::callId``
    对号，动一个号就全乱（方案 §3.5.4）。

    只认 ``DEDUPE_FIELDS`` 登记的「工具 + 字段」，且只认**足够长的字符串**：
    不认识的、短的、非字符串的统统原样放过。将来加新工具不会误伤。

    **纯函数**：不改入参。``messages()`` 有缓存，就地改会污染缓存里的对象。
    """
    hits: list[tuple[int, int, str, str]] = []  # (消息下标, 块下标, 字段名, 值)
    for msg_idx, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        # ⚠️ 用 blocks 的下标，不是 tool_calls 的下标：后者是过滤后的视图，
        # 前面夹了文本块时两者会错位。
        for block_idx, block in enumerate(message.blocks):
            if not isinstance(block, ToolCallBlock):
                continue
            fields = DEDUPE_FIELDS.get(block.name)
            if not fields:
                continue
            for field in fields:
                value = block.arguments.get(field)
                if isinstance(value, str) and _byte_len(value) >= DEDUPE_MIN_BYTES:
                    hits.append((msg_idx, block_idx, field, value))

    if not hits:
        return list(messages)

    # 同一个值只在**最后一次出现**处保留原样，其余降为存根
    last_at: dict[str, int] = {}
    for pos, (_, _, _, value) in enumerate(hits):
        last_at[value] = pos
    stubbed = {pos for pos, (_, _, _, value) in enumerate(hits) if last_at[value] != pos}
    if not stubbed:
        return list(messages)

    # 按「消息 → 块」归拢，一次重建一条消息，避免同一条消息被反复复制
    pending: dict[int, dict[int, list[str]]] = {}
    for pos, (msg_idx, block_idx, field, _) in enumerate(hits):
        if pos in stubbed:
            pending.setdefault(msg_idx, {}).setdefault(block_idx, []).append(field)

    result = list(messages)
    for msg_idx, blocks_to_fix in pending.items():
        message = result[msg_idx]
        if not isinstance(message, AssistantMessage):
            continue
        blocks = list(message.blocks)
        for block_idx, fields in blocks_to_fix.items():
            block = blocks[block_idx]
            if not isinstance(block, ToolCallBlock):
                continue
            arguments = dict(block.arguments)
            for field in fields:
                arguments[field] = DEDUPE_STUB
            blocks[block_idx] = block.model_copy(update={"arguments": arguments})
        result[msg_idx] = message.model_copy(update={"blocks": blocks})
    return result


def dedupe_safely(messages: Sequence[Message]) -> list[Message]:
    """``dedupe`` 的兜底包装：**压缩永不成为新的异常源**（方案 §6.2 硬约束 1）。

    去重是纯渲染，理论上不该抛；但它的输入是模型给的工具参数，形态不可控。真出意外
    就原样退回——宁可这一轮不省这点体积，也绝不因为压缩把整轮搞崩。
    """
    try:
        return dedupe(messages)
    except BaseException:
        # 刻意兜住一切（含 CancelledError）：这里是渲染路径，不是业务流程。
        _LOGGER.warning("上下文去重失败，本轮按原样发送", exc_info=True)
        return list(messages)


# ---------- 决策层：字节 → token 保守估算（方案 §6.1） ----------


def estimate_tokens_from_bytes(
    total_bytes: int,
    *,
    bytes_per_token: float = DEFAULT_BYTES_PER_TOKEN,
    safety: float = DEFAULT_ESTIMATE_SAFETY,
) -> int:
    """把本地称出来的 UTF-8 字节数保守换算成 token。

    真实 token 数只有模型回传 input_tokens 才知道，且滞后一轮；决策不能干等，
    先用字节估。英文 / JSON 经验约 3.5 字节/token，中文按 3.5 裸除会少估，
    所以再乘安全上浮系数（默认 1.15），方向是**宁早压、不晚压**，最后向上取整。

    系数是初始猜测，上线后用真实「字节 ↔ input_tokens」成对数据校准（方案 §18）。
    """
    if total_bytes <= 0:
        return 0
    return math.ceil(total_bytes / bytes_per_token * safety)


def composition_tokens(
    stats: ContextStats,
    *,
    bytes_per_token: float = DEFAULT_BYTES_PER_TOKEN,
    safety: float = DEFAULT_ESTIMATE_SAFETY,
) -> dict[str, int]:
    """把一次称重（:func:`measure`）的各桶字节，逐桶保守换算成估算 token。

    给压缩遥测的「窗口构成账」用：压缩触发时记一笔**压前窗口被哪类内容占着**
    （说明书 / 每轮注入 / 用户 / 助手 / 工具参数 / 成品结果 / 脚手架结果），
    纯本地估算、不发一次额外请求，也不进用户界面。

    返回键与 :meth:`ContextStats.parts` 完全一致。口径是本地字节的保守估算
    （宁高不低），只用于看相对占比，不与模型实测 ``input_tokens`` 对齐；各桶逐桶
    向上取整，故各桶之和可能比"总字节一次换算"多几个 token（每桶不足 1 的取整，
    总量差不超过桶数），属正常现象。
    """
    return {
        key: estimate_tokens_from_bytes(size, bytes_per_token=bytes_per_token, safety=safety)
        for key, size in stats.parts().items()
    }


# ---------- 决策层：三态决策（方案 §6.1 / §6.4） ----------

# 对齐 Claude Code 的三态命名（方案 D1）：
#   normal  —— 直接发；
#   compact —— 发送前先按 levels 做主动压缩（l2 免费装箱 / l3 花钱摘要）；
#   blocked —— 已到硬阻断线，不压必 413，本轮发送前必须先走应急减重（§10）。
STATE_NORMAL = "normal"
STATE_COMPACT = "compact"
STATE_BLOCKED = "blocked"


@dataclass(frozen=True)
class CompactionDecision:
    """一轮发送前的压缩决策。**纯数据、只读**：调用方（loop）照单执行。"""

    state: str
    # compact 时为 ("l2",) 或 ("l2", "l3")；normal / blocked 时为空元组。
    levels: tuple[str, ...]
    # 预判的下一轮输入水位（token），连同窗口一起回传，供事件 / 遥测 / 调试核对。
    predicted_tokens: int
    window_tokens: int
    # 人话原因，写给日志和前端状态，不参与任何判断。
    reason: str


def decide_compaction(
    *,
    effective_window: int,
    last_input_tokens: int | None,
    last_output_tokens: int | None,
    estimated_tokens: int,
    new_user_estimated_tokens: int = 0,
    l2_active: bool = False,
    auto_compact: bool = True,
    pct_l2: float = DEFAULT_PCT_L2,
    reserve_compact: int = DEFAULT_RESERVE_COMPACT,
    reserve_hard: int = DEFAULT_RESERVE_HARD,
) -> CompactionDecision:
    """决定本轮发送前处于 normal / compact / blocked 哪个状态。**纯函数**。

    水位口径（方案 §6.1，A2 修订）：
      - 有真实 usage：``上轮 input + 上轮 output + 本轮新增 user``。
        上轮输出下一轮会**全额回灌成输入**，升档后可达 65 536，漏记这笔会直接压晚；
      - 无真实 usage（首轮 / 服务商没回）：退回 ``estimated_tokens``（全量消息的本地
        字节换算，天然含已入账输出与本轮新增）。此时没有可信尺子，按方案 §6.4 **只做
        L1**——不主动压缩、也不主动阻断；首轮真撞 413 由适配层走被动应急（§10）。

    比较顺序（方案 §6.4）：硬阻断最先判，保命不受 ``auto_compact`` 开关影响（C3）；
    开关关闭时跳过主动 L2/L3；否则按 L2 线、L3 线依次定级。``l2_active`` 提供粘滞：
    L2 一旦启用，水位即使短暂回落也保持装箱，避免反复横跳。

    本函数**无状态**：不记录"这轮是否已摘要过"。"一轮最多一次 L3"由调用方（loop，
    #66 接线）保证——每轮发送前只调一次，而不是放进重试循环里反复触发。
    """
    if last_input_tokens is None:
        return CompactionDecision(
            STATE_NORMAL,
            (),
            estimated_tokens,
            effective_window,
            "首轮或缺少真实 input_tokens，只用本地估算，按方案只做 L1，不主动压缩",
        )

    predicted = last_input_tokens + (last_output_tokens or 0) + new_user_estimated_tokens

    hard_line = effective_window - reserve_hard
    compact_line = effective_window - reserve_compact
    # 截断取整让线略低一点点：边界模糊时宁可早压半拍。
    l2_line = int(effective_window * pct_l2)

    # 硬阻断最先判：这是保命，不受主动压缩开关影响（方案 C3）——
    # 用户关掉的是"花钱花动作的主动压缩"，不是"允许请求超长被卡死"。
    if predicted >= hard_line:
        return CompactionDecision(
            STATE_BLOCKED,
            (),
            predicted,
            effective_window,
            f"预判水位 {predicted} 已到硬阻断线 {hard_line}，发送前必须先应急减重",
        )

    # 开关关闭：主动 L2/L3 一律不做（真撞 413 仍由适配层应急，与本决策无关）。
    if not auto_compact:
        return CompactionDecision(
            STATE_NORMAL,
            (),
            predicted,
            effective_window,
            f"主动压缩已关闭，预判水位 {predicted} 不作压缩（413 应急仍生效）",
        )

    levels: list[str] = []
    # L2 粘滞：本会话已启用装箱就保持，哪怕水位短暂跌回线下（避免反复横跳）。
    if predicted >= l2_line or l2_active:
        levels.append("l2")
    if predicted >= compact_line:
        levels.append("l3")

    if not levels:
        return CompactionDecision(
            STATE_NORMAL,
            (),
            predicted,
            effective_window,
            f"预判水位 {predicted} 未到任何压缩线（L2 线 {l2_line}）",
        )

    return CompactionDecision(
        STATE_COMPACT,
        tuple(levels),
        predicted,
        effective_window,
        f"预判水位 {predicted} 触发 {', '.join(levels).upper()}（L2 线 {l2_line} / "
        f"L3 线 {compact_line} / 硬阻断线 {hard_line}）",
    )
