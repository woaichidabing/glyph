"""错误可重试判定 —— 全项目唯一一处（R1 配置单点）。

为什么要单独抽这个文件
----------------------
这条规则原本被抄了三份：`llm/qwen.py`、`llm/responses.py`、`tools/grader.py`。
抄三遍的直接后果有两个：

1. 想加一条新规则要改三个地方，改漏一处就会出现「主对话能自愈、批改工具不会自愈」
   这种说不通的怪事；
2. 其中两份的写法藏着同一个坑 —— ``status = getattr(exc, "status_code", 0)``
   之后直接比较 ``status >= 500``。当 ``status_code`` **属性存在但值为 None** 时
   （某些兼容客户端、测试假客户端会这样），``None >= 500`` 抛 TypeError，
   一个判定函数反倒把调用方搞崩了。

现在三处一律引用这里。

参照过谁
--------
Codex 把判定集中在 ``codex-rs/protocol/src/error.rs::CodexErr::retry_delay``，
一个 match 同时回答「能不能重试」和「等多久」（返回 None 即不可重试）。
我们这里只取它的**集中一处**这一点，返回值仍用 bool —— 三处调用点写法不用动，
改动面最小（本项目单用户本地运行，暂无必要引入"返回一个时长"的额外概念）。

已知边界（诚实标注）
--------------------
Codex 会区分「配额耗尽 QuotaExceeded」与「临时限流 RateLimitExceeded」，
前者重试纯属白等。HTTP 层两者都可能表现为 429，``is_retryable`` 只判到状态码；
但千问 Responses 会在 SSE 里发 ``response.failed``、错误体带 code/message，
那种没有状态码的逻辑错误由 ``is_error_body_retryable`` 按文字判（配额/欠费/鉴权
不重试）。2026-09-27 已拿到 "Free quota exhausted" 真实样本并据此实现。
"""

from __future__ import annotations

import re

from .llm.base import ContextTooLongError

RETRYABLE_STATUS_MIN = 500
"""5xx 一律可重试（服务端临时性故障）。"""

RETRYABLE_STATUS_EXTRA = frozenset({429})
"""429 = 太挤了，等一会儿再试通常就好。"""


def is_retryable(exc: Exception) -> bool:
    """判断一个异常值不值得重试。

    可重试：连接失败 / 超时 / 429 / 5xx。
    不可重试：其余一切 —— 尤其是 4xx 与鉴权失败（重试一万次也是同一个错）。

    两处都是以前真踩过的坑，别改回去：
    - ``status_code`` 可能是 None，先确认它是 int，否则 ``None >= 500`` 会抛 TypeError；
    - openai 是可选依赖，导入失败不能让判定本身崩掉（有鸭子类型兜底）。
    """
    try:
        import openai

        connection_errors = (
            getattr(openai, "APIConnectionError", ()),
            getattr(openai, "APITimeoutError", ()),
        )
        if isinstance(exc, connection_errors):
            return True
    except ImportError:  # pragma: no cover - openai 为可选依赖
        pass

    # Python 内置的连接 / 超时异常：部分客户端（自建 transport、流式读取中途掉线）不经过
    # openai 的包装，把原始异常直接抛上来。它们与 APIConnectionError 是同一类掉线，
    # 必须一起认——否则「同一类网络故障」会因为从哪一层冒出来而得到两种待遇
    # （2026-10-14：讲题工具真机拿到裸 ConnectionError("reset")，判成不可重试直接失败）。
    # 刻意只列这两个：再往上收 OSError 会误吞"文件不存在 / 磁盘满"等本地错误。
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        # 不是整数（None / 字符串 / 根本没有这个属性）：无从判断，按不可重试处理
        return False
    return status in RETRYABLE_STATUS_EXTRA or status >= RETRYABLE_STATUS_MIN


# ---------- 平台错误体（response.failed，无 HTTP 状态码） ----------

# 这些情形不充值 / 不改 key，重试多少次都是同一个错：配额、欠费、鉴权。
NON_RETRYABLE_ERROR_HINTS = frozenset(
    {
        "quota",
        "exhausted",
        "insufficient",
        "billing",
        "add funds",
        "unauthorized",
        "forbidden",
        "invalid api key",
    }
)


def is_error_body_retryable(error: dict) -> bool:
    """判定平台在 ``response.failed`` 里给的错误体（``{"code","message"}``）是否值得重试。

    与 ``is_retryable`` 的区别：这是 SSE 正文里的逻辑错误，不是 HTTP 异常、
    没有 ``status_code``，只能按 code/message 的文字判。配额耗尽 / 欠费 / 鉴权失败
    → False（重试无意义）；其余 server_error / overloaded 等临时故障 → True。

    2026-09-27 拿到的首个真实样本：code=server_error（像临时错误），但 message 是
    "Free quota exhausted ... add funds" —— 真相在 message，所以 code/message
    必须一起看，不能只认 code。
    """
    code = str(error.get("code", "")).lower()
    message = str(error.get("message", "")).lower()
    blob = f"{code} {message}"
    return not any(hint in blob for hint in NON_RETRYABLE_ERROR_HINTS)


# ---------- 上下文超长（§10：区别于普通网络抖动，必须先减重再重发） ----------

# HTTP 413 是标准"请求体过大"；部分厂商把 context 超长错放在 400 里、靠错误文案区分。
CONTEXT_TOO_LONG_STATUS = frozenset({400, 413})

# 各家厂商"上下文超长"的典型文案（OpenAI / DashScope / 兼容网关），统一小写匹配。
CONTEXT_TOO_LONG_HINTS = frozenset(
    {
        "context_length_exceeded",
        "context length",
        "maximum context",
        "maximum_context_length",
        "maximum context length",
        "max_tokens",
        "too long",
        "input length",
        "prompt is too long",
        "request too large",
        "reduce the length",
        "messages are too long",
    }
)


def is_context_too_long(exc: Exception) -> bool:
    """判断异常是不是"上下文超长"（§10），它**不是**可无限重试的网络抖动。

    判定优先级（任何一条命中即算，且都要求不误伤 5xx）：

    - 已经是 :class:`ContextTooLongError`：直接认；
    - HTTP 413：标准请求过大，直接认；
    - 400/413 且错误文案含超长线索：认（部分厂商把它放 400）；
    - 没有整数状态码（厂商 SDK 只给文字）且文案含超长线索：认；
    - 其余（含 5xx，哪怕文案偶然出现 "too long"）：不认——5xx 交给网络重试。

    与 :func:`is_retryable` 互斥使用：超长要的是"先减重、再重发"，不是"原样再试"。
    """
    if isinstance(exc, ContextTooLongError):
        return True
    status = getattr(exc, "status_code", None)
    if status == 413:
        return True
    blob = str(exc).lower()
    has_hint = any(hint in blob for hint in CONTEXT_TOO_LONG_HINTS)
    if not has_hint:
        return False
    if isinstance(status, int):
        # 有明确状态码：只在 400/413 认；5xx 等即使文案撞词也按网络故障走，避免误吞重试
        return status in CONTEXT_TOO_LONG_STATUS
    # 无状态码、只有文字：认（openai 部分兼容客户端不挂 status_code）
    return True


# ---------- 配额 / 欠费 / 鉴权：同一个模型重试无意义，正确动作是"换模型" ----------

# 这一档状态码专指账号 / 权限 / 付费问题。刻意不含 429：429 是临时挤爆，
# 同一个模型等一会儿再试通常就好，不该因此换模型。
QUOTA_OR_AUTH_STATUS = frozenset({401, 402, 403})

# 错误文案线索：英文沿用 NON_RETRYABLE_ERROR_HINTS，另补国内兼容网关常见中文说法。
QUOTA_OR_AUTH_HINTS = frozenset(
    {
        "quota",
        "exhausted",
        "insufficient",
        "billing",
        "add funds",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "额度",
        "配额",
        "欠费",
        "余额",
        "充值",
        "鉴权",
        "无权",
        "没有权限",
    }
)


def _fold_hint_text(text: str) -> str:
    """抹掉分隔符（空格 / 连字符 / 下划线）并转小写，再拿去比对线索。

    平台对同一个错有多种写法：``Invalid API key`` / ``Invalid API-key`` /
    ``InvalidApiKey``。分隔符不同不该改变结论，抹平之后三者是同一个串，判一次就够——
    不必为每种写法各补一条线索（2026-10-12 真机 401 就是这么漏掉的：适配层包装后
    只剩一段文案，而线索写的是带空格的那一种）。
    """
    return re.sub(r"[\s\-_]+", "", text.lower())


_QUOTA_OR_AUTH_FOLDED_HINTS = frozenset(_fold_hint_text(hint) for hint in QUOTA_OR_AUTH_HINTS)


def is_quota_or_auth_error(exc: Exception) -> bool:
    """判断异常是不是"这个模型用不了"——没额度 / 欠费 / 没权限 / key 不对。

    与 :func:`is_retryable` 的区别：网络重试是"同一个模型再试一次"，这里的错重试一万次
    还是同一个错，正确动作是**换一个模型**（见 ``llm/fallback.py``），不是干等。

    - 包装时带来的标记 ``exc.quota_or_auth``：认。适配层会把原始异常包成
      ``LLMError``，状态码在那一步就没了；包装方在**还有原始异常**时按本函数算好结论
      带上来，这里直接采信——否则主循环只看得到一段英文文案，既换不了脑、还会把原文
      甩给用户（2026-10-12 真机 401）；
    - 状态码 401/402/403：认（本项目网关实测 403 就是 FreeTierOnly / 配额耗尽）；
    - 没有状态码、只有错误文字：命中配额 / 欠费 / 鉴权线索（分隔符不敏感）也认；
    - 429 不认（临时限流，同模型重试即可）；
    - ``status_code`` 可能是 None，先确认是 int，避免 None 比较出 TypeError。
    """
    if getattr(exc, "quota_or_auth", False):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in QUOTA_OR_AUTH_STATUS:
        return True
    blob = _fold_hint_text(str(exc))
    return any(hint in blob for hint in _QUOTA_OR_AUTH_FOLDED_HINTS)
