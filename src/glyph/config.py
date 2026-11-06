"""配置单点（铁律 R1）。

key / base_url / model 只从这里读：优先环境变量，启动时从项目根的 ``.env`` 加载
（零依赖极简解析）。``.env`` 已在 .gitignore，绝不入库；代码里不写死任何密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .storage import data_root

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 主对话（接待 + 编排 + 短引导）模型：编排可靠性优先，默认强模型
DEFAULT_MODEL = "deepseek-v4-pro"
# grade_text 工具内部批改主力（重判断、长输出），可单独配置
DEFAULT_GRADE_MODEL = "deepseek-v4-pro"
# 批改/复核/写作分析子模型的自动降级顺序：首选 grade_model 撞上配额/欠费/鉴权时，
# 按此顺序换"同一个兼容端点"上的模型各试一次（只换 model 名、不换 key/base_url）。
# deepseek-v4.1-flash 实测在该端点稳定可用、批改质量够用，适合当兜底。
DEFAULT_GRADE_FALLBACK_MODELS = ("deepseek-v4.1-flash",)
# 主脑（对话大脑）本轮零产出就撞配额/欠费/鉴权时的自动兜底顺序：只换 model 名、
# 不换 key/base_url（catalog 里的模型都在同一个千问兼容网关、用同一把 key）。
# kimi-k3 实测满额可用、支持思考三档与联网、长文本稳，适合当"主脑没额度"的第一替补。
DEFAULT_MAIN_FALLBACK_MODELS = ("kimi-k3",)
# 润色升档（polish_text）的子模型：**刻意不与批改共用**。
# 2026-10-08 真机 A/B（设计文档 §12）：拿一段"本来就写得好"的英文考 15 个候选，
# qwen3.7-plus 老实说"没得改"且评语读懂了修辞；而 flash 系（含批改在用的
# qwen3.5-flash / 主脑 deepseek-v4.1-flash）会硬挑毛病——"换了但没更好"，
# 正是"升档≠堆难词"这条红线。两者的合格线不同，故单开一个旋钮。
DEFAULT_POLISH_MODEL = "qwen3.7-plus"
# 单轮输出上限：先用基础值，模型输出被截断（finish_reason=length）时静默升到升级值
DEFAULT_MAX_TOKENS = 8192
DEFAULT_MAX_TOKENS_UPGRADE = 65536
# 流式空闲超时（秒）：相邻两个事件之间静默超过该值，即判定"连接还在但服务端卡住"，
# 自动重发当前轮。应短于模型客户端的读取超时（装配处 openai client timeout），作为更紧、
# 带专门提示的第一道自救；<=0 可在构造 loop 时禁用。
DEFAULT_STREAM_IDLE_TIMEOUT = 90.0
# 单个工具的墙钟上限（秒）：超过就补一条"超时"失败结果，把决定权交回模型
# （换工具 / 拆小任务 / 直接回答），而不是让一整轮永远悬着、UI 一直转圈。
# 120 秒的取值理由：本地工具（批改、拼写一类的模型调用）正常都在十秒内返回，
# 120 秒足够宽松，又不会让用户在"什么都看不到"的状态下干等两分钟以上。
# <=0 表示不设上限（交回工具自己保证，不推荐）。
DEFAULT_TOOL_TIMEOUT = 120.0
# 同时活着的会话运行时的上限（会话池）：一个会话一份 history + 一个 AgentLoop 实例。
# 4 的取值理由：本地单用户，正常就是开两三个标签页；再往上每个会话都要占一份流水句柄
# 与一份循环状态，收益远小于内存代价。超了淘汰**最久没被用过且不在跑**的那个（只清内存，
# 流水文件不动，下次打开还能恢复）。
DEFAULT_SESSION_POOL_MAX = 4
# 视觉（转写）模型：只做「图片 → 英文文本」的抄写，绝不批改。
# 取值来自真样张冒烟（10 张手写作文拍照，2026-09-23）：
# - qwen3-vl-flash：转写最忠实（不擅自改原文的字）、基本无夹带、1–2 秒/张、最省 → 选它；
# - qwen3-vl-plus：更干净但会擅自润色（把原文的 late 悄悄改成 ate、小写开头改成大写），
#   这类"悄悄改对"最危险——批改阶段就看不见这个错误了，等于把真实错误藏起来；
# - qwen-vl-ocr-latest：OUT。它会把 App 界面按钮、用户真名、分数一并抄进输出（隐私不可接受）。
DEFAULT_VISION_MODEL = "qwen3-vl-flash"
# 单张图片的字节上限：超过就直接报错而不是硬塞给视觉模型（守 R5，把决定权交回人和模型）。
# 10MB 的量级沿用旧实现；正常作文截图都在 1MB 内。
DEFAULT_OCR_MAX_BYTES = 10 * 1024 * 1024
# 复核（使用者驳回一条 → AI 拿上下文重判）的三道闸门阈值：
# - 单条：同一条最多复核 2 次，第 3 次驳回不给判，直接退化为「剔除」；
# - 单篇：一篇作文累计 5 次，用完就不再有复核入口（单篇由 web/ 拦，见 C-3，
#   这里只是把数字放在配置单点，避免两边各写一份 2 和 5）；
# - 上下文硬顶：超过就按最近句尾左截，防止「整篇只有一段」退化成整篇重批。
DEFAULT_RECHECK_MAX_ROUNDS = 2
DEFAULT_RECHECK_MAX_TOTAL = 5
DEFAULT_RECHECK_MAX_CONTEXT_CHARS = 4096
# 句子不足多少个词就升级到段落：短句里判断不了指代、跨句时态、指代对象。
# 8 词的取值理由：一句完整简单句（主谓宾 + 少量修饰）差不多就是这个量级。
DEFAULT_RECHECK_MIN_CONTEXT_WORDS = 8
# 长期记忆六个旋钮（设计稿 §3.9）：
# - 目录页每行字数 / 条数 / 整篇字节：对齐 Claude Code 目录约 150 字符、
#   Codex AGENTS.md 32 KiB 上限；
# - 单条正文字节：read_memory 一次最多读 4 KiB；
# - fact 未提起天数：只有 fact 类 30 天没被提起就退出目录（mistake 永不按时淘汰）；
# - title 字数：软约束，超了警告不拒绝。
DEFAULT_MEMORY_LINE_MAX_CHARS = 150
DEFAULT_MEMORY_CATALOG_MAX_ENTRIES = 50
DEFAULT_MEMORY_CATALOG_MAX_BYTES = 32 * 1024
DEFAULT_MEMORY_BODY_MAX_BYTES = 4 * 1024
DEFAULT_MEMORY_FACT_UNUSED_DAYS = 30
DEFAULT_MEMORY_TITLE_MAX_CHARS = 40

# 长期记忆 v2：后台提炼与整理（D27 / D29，设计稿 memory-v2-implementation-spec.md）。
# 取值来源与取舍：
# - 提炼触发对齐 Claude Code 桌面端（every_n_turns=5 / idle_flush_s=120），
#   但空闲阈值放宽到 300 秒——CC 是订阅制，Glyph 用户自备 key，按 token 付费；
# - 提炼器往返上限 4 来自 CC 的 max_turns（防子代理反复读、就是想不出要记什么）；
# - 整理门槛 24 小时 + 3 个会话来自 CC 的 {minHours: 24, minSessions: 5}，
#   会话数降到 3：Glyph 单用户使用频率低于多仓库的开发者；
# - 节流 600 秒同样是 CC 的值，防反复扫描；
# - 提炼/整理复用压缩写手那个便宜模型（不是主脑）。
DEFAULT_MEMORY_EXTRACT_EVERY_TURNS = 5
DEFAULT_MEMORY_EXTRACT_IDLE_SECONDS = 300
DEFAULT_MEMORY_EXTRACT_MAX_ROUNDS = 4
DEFAULT_MEMORY_CONSOLIDATE_MIN_HOURS = 24
DEFAULT_MEMORY_CONSOLIDATE_MIN_SESSIONS = 3
DEFAULT_MEMORY_CONSOLIDATE_THROTTLE_SECONDS = 600

# T07 学习尝试记录（~/.markr/attempts）：read_attempts 一次回灌的上限。
# - 条数：一次只看最近 5 次尝试，更多靠缩小知识点再读；
# - 总字节：单工具回灌预算 8 KiB，比记忆目录更紧，避免把上下文撑爆。
DEFAULT_ATTEMPTS_READ_LIMIT = 5
DEFAULT_ATTEMPTS_TOTAL_MAX_BYTES = 8 * 1024

# 平台内置 web_search 默认随请求声明（T02 的语义：与本地 function 混用，由模型自主挑）。
# 支持它的主模型（如 qwen3.7-plus / deepseek-v4-pro）照旧打开；
# 不支持的（实测 deepseek-v3.2-exp 会 400）用 GLYPH_WEB_SEARCH=false 关掉。
DEFAULT_WEB_SEARCH = True

# 主脑思考档（Responses 的 reasoning.effort 参数，千问 / DeepSeek / GLM 三家都认）：
# 低 / 中 / 高三档，开箱默认"中"（均衡）。产品刻意**不做**"自动"档与"关闭"档
# （用户拍板）；环境变量配了不认的值也回退到"中"，绝不把模型不认的档位发出去。
# 供应商协议（与前端 provider.api 对齐）：决定装配层选哪条 LLM 通道
API_PROTOCOL_CHAT = "openai-chat"
API_PROTOCOL_RESPONSES = "openai-responses"
API_PROTOCOL_ANTHROPIC = "anthropic-messages"
API_PROTOCOLS = (API_PROTOCOL_CHAT, API_PROTOCOL_RESPONSES, API_PROTOCOL_ANTHROPIC)

REASONING_LOW = "low"
REASONING_MEDIUM = "medium"
REASONING_HIGH = "high"
# 2026-10-27 扩表：个别网关还有"极高 / 最高"两档（xhigh / max）。默认三档不变，
# 只有模型自己声明支持时才多出来 —— 见 REASONING_LEVELS_ALL（词表）与
# REASONING_LEVELS（内置模型的默认三档）。
REASONING_OFF = "off"
REASONING_XHIGH = "xhigh"
REASONING_MAX = "max"
REASONING_LEVELS_ALL = (
    REASONING_OFF,
    REASONING_LOW,
    REASONING_MEDIUM,
    REASONING_HIGH,
    REASONING_XHIGH,
    REASONING_MAX,
)
REASONING_LEVELS = (REASONING_LOW, REASONING_MEDIUM, REASONING_HIGH)
DEFAULT_REASONING_EFFORT = REASONING_MEDIUM

# 上下文窗口档位（用户拍板）：默认「标准」30 万 token，需要长对话 / 整本材料时手动开
# 「Max」100 万。四个在表大脑的官方窗口都是 1M，标准档是产品**刻意**把"上下文上限 /
# 压缩时机"提前到 30 万 —— 日常英语学习（对话、作文、短练习）远用不到，更早压缩更省、
# 上下文更聚焦，而不是模型只能装 30 万；Max 档才放到模型真实的 1M，更晚压缩、少丢前文。
# 该档只作用于主脑的输入侧窗口，不换模型、不影响批改 / 看图等子模型，也不改单轮输出上限。
CONTEXT_MODE_STANDARD = "standard"
CONTEXT_MODE_MAX = "max"
CONTEXT_MODES = (CONTEXT_MODE_STANDARD, CONTEXT_MODE_MAX)
CONTEXT_WINDOW_STANDARD = 300_000
CONTEXT_WINDOW_MAX = 1_000_000
DEFAULT_CONTEXT_MODE = CONTEXT_MODE_STANDARD

# 说话风格（用户拍板只做三档；设置项名"说话风格"，归设置「助手」分类）：
# 自然（默认）/ 风趣 / 冷静。它只换系统提示词里 tone 段的口吻，**不改任何业务结论**
# （分数、错误判定、正确答案、工具流程、确认闸都不受风格影响，即"换皮不换料"）。
# 各档口吻的具体文案在 agent_prompt._TONE_PERSONAS：新增 / 改名档位时两处一起改。
SPEAKING_STYLE_NATURAL = "natural"
SPEAKING_STYLE_WITTY = "witty"
SPEAKING_STYLE_CALM = "calm"
SPEAKING_STYLES = (SPEAKING_STYLE_NATURAL, SPEAKING_STYLE_WITTY, SPEAKING_STYLE_CALM)
DEFAULT_SPEAKING_STYLE = SPEAKING_STYLE_NATURAL


def context_window_cap(mode: str) -> int:
    """产品**档位**的上下文窗口上限（token）：standard 30 万 / max 100 万。

    这是与具体模型无关的配置量（"模型自身窗口更小要再收紧"在
    ``llm.models.context_window_of`` 做，那层才知道模型）。session 层算压缩预算只认
    档位，刻意不 import llm——B2 依赖铁律：压缩 / 会话层不得反向依赖模型适配层。
    """
    return CONTEXT_WINDOW_MAX if mode == CONTEXT_MODE_MAX else CONTEXT_WINDOW_STANDARD


# 上下文去重（压缩 L1）：同一份材料在上下文里只留最近一份。
# 零损失（重复就是重复），所以默认一直开；留这个开关是为了真出意外时能一键退回原样，
# 而不是为了"要不要省这点体积"。
DEFAULT_CONTEXT_DEDUPE = True

# ---------- 上下文压缩决策层（方案 §6，v2.1 #62） ----------
# 本地字节 → token 的保守换算：真实 token 数要等模型回传 input_tokens 才知道，
# 决策不能每轮都等滞后一轮的真实值，先用字节估。英文 / JSON 经验约 3.5 字节/token，
# 中文一个字 3 字节却常算 1–2 token，按 3.5 裸除会**系统性少估**、导致压晚了；
# 所以再乘一个安全上浮系数，原则是"宁早压、不晚压"。两个值都是初始猜测，
# 用真实会话「字节 ↔ input_tokens」成对数据校准（方案 §18 探针）。
DEFAULT_BYTES_PER_TOKEN = 3.5
DEFAULT_ESTIMATE_SAFETY = 1.15

# 主动 L2/L3 总开关（GLYPH_AUTO_COMPACT），默认开。
# 注意语义边界（方案 §10 / C3）：它**只管主动的 L2 装箱与 L3 摘要**；
# 413 应急是保命逻辑，任何开关取值下都生效——用户关压缩不等于允许请求被超长卡死。
DEFAULT_AUTO_COMPACT = True

# 四条决策线（方案 §6.3，W = 有效窗口）：
# - L2 线：W × PCT_L2，过线启用免费、可恢复的成品装箱；
# - L3 线：W − RESERVE_COMPACT，过线才花钱调一次便宜模型写摘要；
# - 硬阻断线：W − RESERVE_HARD，过线不等报错、主动走 413 应急；
# 预判水位口径（方案 §6.1 / A2）：上轮 input + 上轮 output（升档可达 65 536）+
# 本轮新增 user——上轮输出下一轮全额回灌成输入，不记这笔会直接漏判。
DEFAULT_PCT_L2 = 0.50
DEFAULT_RESERVE_COMPACT = 90_000
DEFAULT_RESERVE_HARD = 12_000

# L2 成品装箱的渲染参数（方案 §8.2 / §8.3，v2.1 #63）：
# - ANCHOR_DETAIL_RECENT：提货单里最近多少篇给「逐条锚点」（每条 detection 的不可再生
#   锚点都铺开），更早的退成一行；
# - ANCHOR_BUDGET_RATIO：提货单总字节预算 ≤ W × 该比例，超了从最旧的逐条退化、再省略
#   最旧的一行（提货单自己不能堆成新地板，A4）；
# - TASK_IDLE_TURNS：一个批改任务连续多少个完整问答往返没人再碰（无 recheck / 取回）
#   就视为静默关闭（关闭信号 3，兜底）；
# - FOLD_KEEP_RECENT：最近 K 轮保护窗口——脚手架折叠、成品装箱、取回结果折叠都不得动
#   最近 K 轮（保住"正在逐条裁决 / 取回来正在讲解"的场景）。C10 硬校验 K ≥ 3。
DEFAULT_ANCHOR_DETAIL_RECENT = 5
DEFAULT_ANCHOR_BUDGET_RATIO = 0.04
DEFAULT_TASK_IDLE_TURNS = 3
DEFAULT_FOLD_KEEP_RECENT = 3
FOLD_KEEP_RECENT_MIN = 3
# - ARTIFACT_RESULT_KEEP_RECENT：read_artifact 取回结果在上下文里享有的完整保留窗口（§8.4 B3）。
#   同一篇反复取回只留最近一份完整；过 K 轮未再引用就折回一行（成品仍在提货单上、可再取）。
#   默认与 FOLD_KEEP_RECENT 一致（保住"取回来正在讲解"），下限同为 3。
DEFAULT_ARTIFACT_RESULT_KEEP_RECENT = 3

# L3 摘要压缩参数（方案 §9，v2.1 #65；写手经 §18 探针实测选定）：
# - COMPACT_MODEL：写摘要的便宜大窗口模型（不是主脑）。探针结论：qwen3.7-flash 标称
#   100 万窗口、教学金标 9/9、固定小节稳定、一次约 ¥0.1–0.2。它是后台子模型，**不进
#   llm/models.py 的主脑表**，窗口单独用下面的常量记账。
# - COMPACT_MODEL_WINDOW：写手标称输入窗口 S。喂给它的材料预算 = S × MATERIAL_RATIO，
#   留出 system/输出/余量，绝不满打满算（§9.3）。
# - COMPACT_TARGET_RATIO：压缩后 tail（连同固定开销、续接摘要、提货单）想落到主脑窗口
#   W 的目标比例。动态 tail 预算由它反推（§9.4，A3）。
# - COMPACT_MATERIAL_RATIO：喂给写手的材料最多占写手窗口 S 的比例（默认 0.60）；
#   从切点最新一段向旧按完整往返装，装不下的最旧段不喂（三层兜底，不丢事实）。
# - COMPACT_OUTPUT_RESERVE：给写手摘要输出预留的 token（max_tokens 上限估），算 tail
#   预算时从目标里扣掉，避免摘要把 tail 顶回线上。
# - L3_TAIL_MIN_TURNS：动态 tail 无论如何至少保留的完整问答往返数（下限 1）——切点必须
#   落在某条 user 之前，不能把最近的话也切掉。
DEFAULT_COMPACT_MODEL = "qwen3.7-flash"
DEFAULT_COMPACT_MODEL_WINDOW = 1_000_000
DEFAULT_COMPACT_TARGET_RATIO = 0.60
DEFAULT_COMPACT_MATERIAL_RATIO = 0.60
DEFAULT_COMPACT_OUTPUT_RESERVE = 8_000
DEFAULT_L3_TAIL_MIN_TURNS = 2
L3_TAIL_MIN_TURNS_FLOOR = 1

# --- 日志（observability）---
# 级别：控制台与文件共用一档。空 / 非法一律回退 INFO（配错一个字不该让程序起不来）。
# 取值就是标准库那几档：DEBUG / INFO / WARNING / ERROR / CRITICAL。
# 路径：默认 ~/.markr/logs/backend.log（与 sessions / compaction.jsonl 同一家）；
# 显式设成空串 = 关掉文件出口、只留控制台。轮转 2MB×3 封顶在 observability.py 里。
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FILE = str(data_root() / "logs" / "backend.log")


def load_dotenv(path: Path | None = None) -> None:
    """把项目根 .env 里的 KEY=VALUE 注入环境变量（不覆盖已存在的真实环境变量）。"""
    if path is None:
        # src/glyph/config.py 向上三级即 glyph 项目根
        path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    max_tokens_upgrade: int
    grade_model: str
    stream_idle_timeout: float
    tool_timeout: float
    # 会话池上限：同时活着的会话运行时个数（web 层用；runtime 不该关心，所以只放在这里当阈值）。
    session_pool_max: int = DEFAULT_SESSION_POOL_MAX
    # 当前档的协议（内置档恒为 openai-responses）：决定装配层用哪条通道
    api_protocol: str = API_PROTOCOL_RESPONSES
    # 当前模型是否声明了思考档（自定义档按目录里勾的来；内置档由能力表决定）
    reasoning_supported: bool = True
    # 档位 → 发给厂商的参数值（空 = 就用档位名）
    reasoning_values: dict[str, str] = field(default_factory=dict)
    # 当前模型声明支持的档位（自定义档按模型目录里勾的来；空 = 未声明）
    reasoning_levels: tuple[str, ...] = ()
    # 当前模型的标称上下文窗口（0 = 用户没填，装配层按兜底 256K 算）
    model_context_window: int = 0
    # 主模型自己能不能看图（能看就不需要 describe_image，图片直接交给它）
    main_model_can_see_image: bool = False
    # 追加字段一律带默认值：仓库里有两处独立手造 Settings（tests/tools/test_factory.py、
    # tests/llm/test_responses_adapter.py），新字段若必填会当场把它们打断。
    # 批改系子模型（grade/recheck/writing analyzer）首选 grade_model 用不了时的降级顺序。
    grade_fallback_models: tuple[str, ...] = DEFAULT_GRADE_FALLBACK_MODELS
    # 主脑本轮零产出就撞额度/鉴权时的临时兜底脑顺序（只在本次 run 内换，不改用户选择）。
    main_fallback_models: tuple[str, ...] = DEFAULT_MAIN_FALLBACK_MODELS
    # 润色升档子模型（**独立于 grade_model**，理由见 DEFAULT_POLISH_MODEL 注释）
    polish_model: str = DEFAULT_POLISH_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    ocr_max_bytes: int = DEFAULT_OCR_MAX_BYTES
    # 复核闸门：单条次数 / 单篇总量 / 上下文字符硬顶（见各 DEFAULT_ 的注释）
    recheck_max_rounds: int = DEFAULT_RECHECK_MAX_ROUNDS
    recheck_max_total: int = DEFAULT_RECHECK_MAX_TOTAL
    recheck_max_context_chars: int = DEFAULT_RECHECK_MAX_CONTEXT_CHARS
    recheck_min_context_words: int = DEFAULT_RECHECK_MIN_CONTEXT_WORDS
    # 长期记忆（全部带默认值，见上方注释）
    memory_line_max_chars: int = DEFAULT_MEMORY_LINE_MAX_CHARS
    memory_catalog_max_entries: int = DEFAULT_MEMORY_CATALOG_MAX_ENTRIES
    memory_catalog_max_bytes: int = DEFAULT_MEMORY_CATALOG_MAX_BYTES
    memory_body_max_bytes: int = DEFAULT_MEMORY_BODY_MAX_BYTES
    memory_fact_unused_days: int = DEFAULT_MEMORY_FACT_UNUSED_DAYS
    memory_title_max_chars: int = DEFAULT_MEMORY_TITLE_MAX_CHARS
    # 长期记忆 v2：后台提炼与整理（D27 / D29 的六个旋钮，全部带默认值）
    memory_extract_every_turns: int = DEFAULT_MEMORY_EXTRACT_EVERY_TURNS
    memory_extract_idle_seconds: int = DEFAULT_MEMORY_EXTRACT_IDLE_SECONDS
    memory_extract_max_rounds: int = DEFAULT_MEMORY_EXTRACT_MAX_ROUNDS
    memory_consolidate_min_hours: int = DEFAULT_MEMORY_CONSOLIDATE_MIN_HOURS
    memory_consolidate_min_sessions: int = DEFAULT_MEMORY_CONSOLIDATE_MIN_SESSIONS
    memory_consolidate_throttle_seconds: int = DEFAULT_MEMORY_CONSOLIDATE_THROTTLE_SECONDS
    # 学习尝试记录（T07，全部带默认值）
    attempts_read_limit: int = DEFAULT_ATTEMPTS_READ_LIMIT
    attempts_total_max_bytes: int = DEFAULT_ATTEMPTS_TOTAL_MAX_BYTES
    # 平台内置 web_search 是否随请求声明（T02：与本地 function 在同一 tools 数组里混用）。
    # 不是所有主模型都支持它：实测 deepseek-v3.2-exp 会直接 400
    # "The current model does not support the web_search tool"。
    # 换模型时若撞上这个错，把它关掉即可，不用改代码。
    web_search: bool = DEFAULT_WEB_SEARCH
    # 主脑思考档（reasoning.effort：low/medium/high）。只作用于主脑这一个模型，
    # 不影响批改 grade_model / 看图 vision_model 等子模型（它们各自独立、用户不可见）。
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    # 上下文窗口档位（standard 标准 30 万 / max 100 万），默认标准。只改"上下文上限 /
    # 压缩时机"，不换模型、不影响批改 / 看图等子模型；四型号官方窗口都是 1M，标准档是
    # 产品刻意把压缩提前到 30 万（更省、更聚焦），不是模型只能装 30 万。
    context_mode: str = DEFAULT_CONTEXT_MODE
    # 说话风格（natural 自然 / witty 风趣 / calm 冷静，默认自然）：只换系统提示词 tone
    # 段的口吻，不改业务结论；设置页「助手」分类热切换，随 runtime 重建从下一轮生效。
    speaking_style: str = DEFAULT_SPEAKING_STYLE
    # 上下文去重（压缩 L1）：复核会把整篇原文反复重发，同一份只留最近一份。
    # 零损失，默认开；万一某个新工具的入参被误判成重复，关掉即退回原样。
    context_dedupe: bool = DEFAULT_CONTEXT_DEDUPE
    # 上下文压缩决策层（方案 §6，v2.1 #62）。阈值含义见各 DEFAULT_ 注释；
    # auto_compact 只管主动 L2/L3，413 应急任何取值下都生效（C3）。
    auto_compact: bool = DEFAULT_AUTO_COMPACT
    compact_pct_l2: float = DEFAULT_PCT_L2
    compact_reserve_compact: int = DEFAULT_RESERVE_COMPACT
    compact_reserve_hard: int = DEFAULT_RESERVE_HARD
    bytes_per_token: float = DEFAULT_BYTES_PER_TOKEN
    estimate_safety: float = DEFAULT_ESTIMATE_SAFETY
    # L2 成品装箱（方案 §8，v2.1 #63）；含义见各 DEFAULT_ 注释。
    anchor_detail_recent: int = DEFAULT_ANCHOR_DETAIL_RECENT
    anchor_budget_ratio: float = DEFAULT_ANCHOR_BUDGET_RATIO
    task_idle_turns: int = DEFAULT_TASK_IDLE_TURNS
    fold_keep_recent: int = DEFAULT_FOLD_KEEP_RECENT
    # read_artifact 取回结果完整保留窗口（§8.4 B3，v2.1 #64）
    artifact_result_keep_recent: int = DEFAULT_ARTIFACT_RESULT_KEEP_RECENT
    # L3 摘要压缩（方案 §9，v2.1 #65）；写手是后台便宜大窗口模型，含义见各 DEFAULT_ 注释。
    compact_model: str = DEFAULT_COMPACT_MODEL
    compact_model_window: int = DEFAULT_COMPACT_MODEL_WINDOW
    compact_target_ratio: float = DEFAULT_COMPACT_TARGET_RATIO
    compact_material_ratio: float = DEFAULT_COMPACT_MATERIAL_RATIO
    compact_output_reserve: int = DEFAULT_COMPACT_OUTPUT_RESERVE
    l3_tail_min_turns: int = DEFAULT_L3_TAIL_MIN_TURNS
    # 日志（observability.py 读）：级别与控制台 / 文件两个出口的路径。
    # 与上面同规矩——带默认值，避免打断手造 Settings 的测试。
    log_level: str = DEFAULT_LOG_LEVEL
    log_file: str = DEFAULT_LOG_FILE


def _flag(raw: str | None, default: bool) -> bool:
    """解析布尔型配置：空一律取默认，避免把缺失当成 False。

    认 "1/true/yes/on"（不分大小写）；其余非空值一律视为 False ——
    宁可关掉一个开关，也不要因为拼错了 "flase" 就静默开着。
    """
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _bounded_int(raw: str | None, default: int, *, minimum: int) -> int:
    """解析整数配置：空 / 非数字 / 低于下限一律回退默认。

    压缩参数非法时绝不能带着危险值启动（例如把最近保护窗口配成 0，会把正在裁决的
    篇目当场装箱）——按"压缩永不成为新异常源、宁保守不激进"回退到安全默认。
    """
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _reasoning_effort(raw: str | None) -> str:
    """解析主脑思考档：只认 low/medium/high（大小写容错），空 / 拼错 / 已砍掉的
    "自动、关闭"档一律回退默认"中"，避免把模型不认的档位静默发出去。"""
    if raw:
        value = raw.strip().lower()
        if value in REASONING_LEVELS:
            return value
    return DEFAULT_REASONING_EFFORT


def _csv_models(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """解析逗号分隔的模型名列表（降级顺序）：空 / 全是空白一律回退默认。"""
    if raw is None or not raw.strip():
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or default


def normalize_context_mode(raw: str | None) -> str:
    """解析上下文窗口档位：只认 standard/max（大小写容错），空 / 拼错 / 数字等非法值
    一律回退默认标准档，绝不静默用一个不存在的档位。"""
    if raw:
        value = raw.strip().lower()
        if value in CONTEXT_MODES:
            return value
    return DEFAULT_CONTEXT_MODE


def normalize_speaking_style(raw: str | None) -> str:
    """解析说话风格：只认 natural/witty/calm（大小写容错），空 / 拼错 / 中文等非法值
    一律回退默认自然档，绝不把一个没有对应口吻文案的档位静默拼进系统提示词。"""
    if raw:
        value = raw.strip().lower()
        if value in SPEAKING_STYLES:
            return value
    return DEFAULT_SPEAKING_STYLE


def load_settings() -> Settings:
    load_dotenv()
    # GLYPH_* 优先：.env 用独立名字，绕开"系统已存在 DASHSCOPE_API_KEY 时 .env 不覆盖"
    # （load_dotenv 刻意不覆盖真实环境变量）导致静默用错 key 的坑；未配置再回退 DASHSCOPE_*。
    # 空串一律视为未配置。
    api_key = os.environ.get("GLYPH_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    base_url = (
        os.environ.get("GLYPH_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL") or DEFAULT_BASE_URL
    )
    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=os.environ.get("GLYPH_QWEN_MODEL") or DEFAULT_MODEL,
        max_tokens=int(os.environ.get("GLYPH_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
        max_tokens_upgrade=int(
            os.environ.get("GLYPH_MAX_TOKENS_UPGRADE") or DEFAULT_MAX_TOKENS_UPGRADE
        ),
        grade_model=os.environ.get("GLYPH_GRADE_MODEL") or DEFAULT_GRADE_MODEL,
        grade_fallback_models=_csv_models(
            os.environ.get("GLYPH_GRADE_FALLBACK_MODELS"), DEFAULT_GRADE_FALLBACK_MODELS
        ),
        main_fallback_models=_csv_models(
            os.environ.get("GLYPH_MAIN_FALLBACK_MODELS"), DEFAULT_MAIN_FALLBACK_MODELS
        ),
        polish_model=os.environ.get("GLYPH_POLISH_MODEL") or DEFAULT_POLISH_MODEL,
        stream_idle_timeout=float(
            os.environ.get("GLYPH_STREAM_IDLE_TIMEOUT") or DEFAULT_STREAM_IDLE_TIMEOUT
        ),
        tool_timeout=float(os.environ.get("GLYPH_TOOL_TIMEOUT") or DEFAULT_TOOL_TIMEOUT),
        session_pool_max=int(os.environ.get("GLYPH_SESSION_POOL_MAX") or DEFAULT_SESSION_POOL_MAX),
        vision_model=os.environ.get("GLYPH_VISION_MODEL") or DEFAULT_VISION_MODEL,
        # 日志：GLYPH_LOG_FILE 显式设空串是**有意义的取值**（关文件出口），
        # 所以这里只能用 get 判断"配没配"，不能像上面那样用 or 回退默认值。
        log_level=os.environ.get("GLYPH_LOG_LEVEL") or DEFAULT_LOG_LEVEL,
        log_file=(
            os.environ["GLYPH_LOG_FILE"]
            if "GLYPH_LOG_FILE" in os.environ
            else DEFAULT_LOG_FILE
        ),
        ocr_max_bytes=int(os.environ.get("GLYPH_OCR_MAX_BYTES") or DEFAULT_OCR_MAX_BYTES),
        recheck_max_rounds=int(
            os.environ.get("GLYPH_RECHECK_MAX_ROUNDS") or DEFAULT_RECHECK_MAX_ROUNDS
        ),
        recheck_max_total=int(
            os.environ.get("GLYPH_RECHECK_MAX_TOTAL") or DEFAULT_RECHECK_MAX_TOTAL
        ),
        recheck_max_context_chars=int(
            os.environ.get("GLYPH_RECHECK_MAX_CONTEXT_CHARS") or DEFAULT_RECHECK_MAX_CONTEXT_CHARS
        ),
        recheck_min_context_words=int(
            os.environ.get("GLYPH_RECHECK_MIN_CONTEXT_WORDS") or DEFAULT_RECHECK_MIN_CONTEXT_WORDS
        ),
        memory_line_max_chars=int(
            os.environ.get("GLYPH_MEMORY_LINE_MAX_CHARS") or DEFAULT_MEMORY_LINE_MAX_CHARS
        ),
        memory_catalog_max_entries=int(
            os.environ.get("GLYPH_MEMORY_CATALOG_MAX_ENTRIES") or DEFAULT_MEMORY_CATALOG_MAX_ENTRIES
        ),
        memory_catalog_max_bytes=int(
            os.environ.get("GLYPH_MEMORY_CATALOG_MAX_BYTES") or DEFAULT_MEMORY_CATALOG_MAX_BYTES
        ),
        memory_body_max_bytes=int(
            os.environ.get("GLYPH_MEMORY_BODY_MAX_BYTES") or DEFAULT_MEMORY_BODY_MAX_BYTES
        ),
        memory_fact_unused_days=int(
            os.environ.get("GLYPH_MEMORY_FACT_UNUSED_DAYS") or DEFAULT_MEMORY_FACT_UNUSED_DAYS
        ),
        memory_title_max_chars=int(
            os.environ.get("GLYPH_MEMORY_TITLE_MAX_CHARS") or DEFAULT_MEMORY_TITLE_MAX_CHARS
        ),
        memory_extract_every_turns=int(
            os.environ.get("GLYPH_MEMORY_EXTRACT_EVERY_TURNS")
            or DEFAULT_MEMORY_EXTRACT_EVERY_TURNS
        ),
        memory_extract_idle_seconds=int(
            os.environ.get("GLYPH_MEMORY_EXTRACT_IDLE_SECONDS")
            or DEFAULT_MEMORY_EXTRACT_IDLE_SECONDS
        ),
        memory_extract_max_rounds=int(
            os.environ.get("GLYPH_MEMORY_EXTRACT_MAX_ROUNDS")
            or DEFAULT_MEMORY_EXTRACT_MAX_ROUNDS
        ),
        memory_consolidate_min_hours=int(
            os.environ.get("GLYPH_MEMORY_CONSOLIDATE_MIN_HOURS")
            or DEFAULT_MEMORY_CONSOLIDATE_MIN_HOURS
        ),
        memory_consolidate_min_sessions=int(
            os.environ.get("GLYPH_MEMORY_CONSOLIDATE_MIN_SESSIONS")
            or DEFAULT_MEMORY_CONSOLIDATE_MIN_SESSIONS
        ),
        memory_consolidate_throttle_seconds=int(
            os.environ.get("GLYPH_MEMORY_CONSOLIDATE_THROTTLE_SECONDS")
            or DEFAULT_MEMORY_CONSOLIDATE_THROTTLE_SECONDS
        ),
        attempts_read_limit=int(
            os.environ.get("GLYPH_ATTEMPTS_READ_LIMIT") or DEFAULT_ATTEMPTS_READ_LIMIT
        ),
        attempts_total_max_bytes=int(
            os.environ.get("GLYPH_ATTEMPTS_TOTAL_MAX_BYTES") or DEFAULT_ATTEMPTS_TOTAL_MAX_BYTES
        ),
        web_search=_flag(os.environ.get("GLYPH_WEB_SEARCH"), DEFAULT_WEB_SEARCH),
        reasoning_effort=_reasoning_effort(os.environ.get("GLYPH_REASONING_EFFORT")),
        context_mode=normalize_context_mode(os.environ.get("GLYPH_CONTEXT_MODE")),
        speaking_style=normalize_speaking_style(os.environ.get("GLYPH_SPEAKING_STYLE")),
        context_dedupe=_flag(os.environ.get("GLYPH_CONTEXT_DEDUPE"), DEFAULT_CONTEXT_DEDUPE),
        auto_compact=_flag(os.environ.get("GLYPH_AUTO_COMPACT"), DEFAULT_AUTO_COMPACT),
        compact_pct_l2=float(os.environ.get("GLYPH_COMPACT_PCT_L2") or DEFAULT_PCT_L2),
        compact_reserve_compact=int(
            os.environ.get("GLYPH_COMPACT_RESERVE_COMPACT") or DEFAULT_RESERVE_COMPACT
        ),
        compact_reserve_hard=int(
            os.environ.get("GLYPH_COMPACT_RESERVE_HARD") or DEFAULT_RESERVE_HARD
        ),
        bytes_per_token=float(os.environ.get("GLYPH_BYTES_PER_TOKEN") or DEFAULT_BYTES_PER_TOKEN),
        estimate_safety=float(os.environ.get("GLYPH_ESTIMATE_SAFETY") or DEFAULT_ESTIMATE_SAFETY),
        anchor_detail_recent=_bounded_int(
            os.environ.get("GLYPH_ANCHOR_DETAIL_RECENT"),
            DEFAULT_ANCHOR_DETAIL_RECENT,
            minimum=1,
        ),
        anchor_budget_ratio=float(
            os.environ.get("GLYPH_ANCHOR_BUDGET_RATIO") or DEFAULT_ANCHOR_BUDGET_RATIO
        ),
        task_idle_turns=_bounded_int(
            os.environ.get("GLYPH_TASK_IDLE_TURNS"), DEFAULT_TASK_IDLE_TURNS, minimum=1
        ),
        # C10：最近保护窗口不得小于 3，非法值回退默认（宁晚装、不装早）。
        fold_keep_recent=_bounded_int(
            os.environ.get("GLYPH_FOLD_KEEP_RECENT"),
            DEFAULT_FOLD_KEEP_RECENT,
            minimum=FOLD_KEEP_RECENT_MIN,
        ),
        # §8.4 B3：取回结果完整保留窗口同样不得小于 3，非法值回退默认。
        artifact_result_keep_recent=_bounded_int(
            os.environ.get("GLYPH_ARTIFACT_RESULT_KEEP_RECENT"),
            DEFAULT_ARTIFACT_RESULT_KEEP_RECENT,
            minimum=FOLD_KEEP_RECENT_MIN,
        ),
        # L3 摘要压缩（方案 §9，v2.1 #65）。写手模型空串回退默认；tail 往返数下限 1。
        compact_model=os.environ.get("GLYPH_COMPACT_MODEL") or DEFAULT_COMPACT_MODEL,
        compact_model_window=int(
            os.environ.get("GLYPH_COMPACT_MODEL_WINDOW") or DEFAULT_COMPACT_MODEL_WINDOW
        ),
        compact_target_ratio=float(
            os.environ.get("GLYPH_COMPACT_TARGET_RATIO") or DEFAULT_COMPACT_TARGET_RATIO
        ),
        compact_material_ratio=float(
            os.environ.get("GLYPH_COMPACT_MATERIAL_RATIO") or DEFAULT_COMPACT_MATERIAL_RATIO
        ),
        compact_output_reserve=int(
            os.environ.get("GLYPH_COMPACT_OUTPUT_RESERVE") or DEFAULT_COMPACT_OUTPUT_RESERVE
        ),
        l3_tail_min_turns=_bounded_int(
            os.environ.get("GLYPH_L3_TAIL_MIN_TURNS"),
            DEFAULT_L3_TAIL_MIN_TURNS,
            minimum=L3_TAIL_MIN_TURNS_FLOOR,
        ),
    )
