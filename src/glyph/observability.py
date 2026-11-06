"""日志接线单点：把标准库 logging 接到「控制台 + 本地轮转文件」两个出口。

设计事实源：`docs/design/realtime-logging-design.md`（2026-10-13 讨论稿）。
本文件只做那篇里已经拍板的事，不额外发明。

## 为什么需要这个模块

在它之前，全仓**没有任何** `logging.basicConfig` / `dictConfig`：root logger 上没有
handler，于是 **INFO 一条都看不到**（低于 `lastResort` 的 WARNING 门槛，直接丢），
WARNING / ERROR 靠 `lastResort` 兜底打出来**没有时间、没有级别、没有来源**。
结果是"出错只能复现、模型变慢只能猜、工具跑没跑只能从界面反推"。

## 三条铁律（跟 `compaction_metrics.py` 同款）

1. **旁路**：日志自身的任何失败（盘满 / 编码 / handler 崩）只影响日志，绝不进主循环、
   不改事件流；
2. **不进协议**：日志不上 SSE、不进会话账本、不上前端——它是开发者工具，不是产品功能；
3. **配置只在入口显式调用**：`import glyph` 不产生任何副作用，测试跑起来行为与从前
   完全一致（这是现有 1800+ 测试不被打扰的关键）。

## 脱敏

`summarize_args` 是**唯一**的参数摘要入口：字符串类值只报长度（`text=512字`），
数字 / 布尔直接报值。这样既能定位"调了什么、传了多大"，又**从机制上**杜绝把作文
原文写进日志——不是靠"记得别打"，是靠"打不出来"。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

from .storage import data_root

__all__ = [
    "BRIEF_MAX_CHARS",
    "DEFAULT_LOG_LEVEL",
    "LOG_FILE_ENV",
    "LOG_LEVEL_ENV",
    "THIRD_PARTY_LOGGERS",
    "brief",
    "configure_logging",
    "default_log_file",
    "summarize_args",
]

LOG_LEVEL_ENV = "GLYPH_LOG_LEVEL"
"""日志级别环境变量名。"""

LOG_FILE_ENV = "GLYPH_LOG_FILE"
"""日志文件路径环境变量名；显式设成空串 = 关掉文件出口，只留控制台。"""

DEFAULT_LOG_LEVEL = "INFO"
"""默认级别：正常路径的打点都按 INFO 设计，默认就该看得见。"""

_LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_LOG_DATE_FORMAT = "%H:%M:%S"

# 轮转上限：2MB × (1 当前 + 3 备份) ≈ 8MB 封顶。日志是用来定位问题的，不是数据仓库，
# 必须有个头，否则跑几个月能把用户的硬盘写满。
LOG_FILE_MAX_BYTES = 2 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 3

# 这些库的 DEBUG 会把 HTTP 请求细节（**含 Authorization 头**）原样打出来，而且极吵。
# 它们**绝不跟随** GLYPH_LOG_LEVEL=DEBUG —— 用户想调试的是 Glyph，不是 httpx 的底层。
THIRD_PARTY_LOGGERS: frozenset[str] = frozenset(
    {
        "httpx",
        "httpcore",
        "httpx2",
        "openai",
        "urllib3",
        "asyncio",
        "multipart",
    }
)

_THIRD_PARTY_LEVEL = logging.WARNING

# 摘要里最多报几个参数，超了折成"...另N个"。防止一行日志变成一屏（工具schema动辄十几个参数）。
_SUMMARY_MAX_KEYS = 8

_GLYPH_LOGGER = "glyph"


def default_log_file() -> Path:
    """默认日志文件：`~/.markr/logs/backend.log`。

    与 `~/.markr/sessions/`、`~/.markr/compaction.jsonl` 同一家（用户 home 下）。
    路径**全 ASCII**，不踩中文路径（R9）。
    """
    return data_root() / "logs" / "backend.log"


def normalize_level(raw: str | None) -> int:
    """把配置里的级别串解析成 logging 常量；空 / 非法一律回退 INFO。

    刻意不抛异常：级别配错一个字就起不来，是比"级别不对"严重得多的问题。
    """
    if raw is None:
        return logging.INFO
    name = str(raw).strip().upper()
    if not name:
        return logging.INFO
    resolved = logging.getLevelName(name)
    # getLevelName 对非法名字返回字符串 "Level XXX"（3.11+ 行为稳定），不是 int
    if isinstance(resolved, int):
        return resolved
    return logging.INFO


def summarize_args(args: Any) -> str:
    """把工具参数压成一行摘要——**脱敏唯一入口**。

    规则（写死在这里，调用方不用各自判断）：

    - **字符串**：只报长度（`text=512字`）。作文原文、用户输入、API key 都从这里
      经过，所以它们永远不会以内容形式出现在日志里；
    - **数字 / 布尔 / None**：直接报值（`max_rounds=2`）——本身没有隐私风险，
      且是定位问题的关键；
    - **容器**：只报条目数（`answers=3项`），**不递归展开**（容器里装的还是内容）；
    - 超过 8 个键截断，折成 `...另N个`。

    返回空串表示"没什么可报的"，调用方据此决定要不要拼这段。
    """
    if not args or not isinstance(args, dict):
        return ""

    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            parts.append(f"{key}={len(value)}字")
        elif isinstance(value, bool):
            parts.append(f"{key}={value}")
        elif isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif value is None:
            parts.append(f"{key}=None")
        elif isinstance(value, (list, tuple, set, dict)):
            parts.append(f"{key}={len(value)}项")
        else:
            parts.append(f"{key}=<{type(value).__name__}>")

    if len(parts) > _SUMMARY_MAX_KEYS:
        hidden = len(parts) - _SUMMARY_MAX_KEYS
        parts = parts[:_SUMMARY_MAX_KEYS] + [f"...另{hidden}个"]

    return " ".join(parts)


# 异常摘要的截断上限：够定位（错误码 / 关键短语）就行。完整现场在异常对象里、
# 也在会话流水里，日志不必（也不该，可能含请求细节）承担全文。
BRIEF_MAX_CHARS = 120


def brief(exc: object, *, limit: int = BRIEF_MAX_CHARS) -> str:
    """把异常压成一行短摘要，供日志用。

    两件事：① 换行折成空格（否则一条日志被撑成多行，grep 与肉眼都难读）；
    ② 超长截断。**不做脱敏**——脱敏靠「根本不把正文传进来」，不是靠事后清洗。
    """
    text = " ".join(str(exc).split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)


def _console_handler() -> logging.StreamHandler:
    """控制台出口（stderr）。

    刻意**不判断 isatty、不加颜色**：Windows 重定向到文件 / 管道时色码会变乱码，
    而 isatty 在这种场景下判断也不可靠（设计稿 §3.2 已定：需要颜色以后再加）。
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_make_formatter())
    return handler


def _file_handler(path: Path) -> logging.Handler | None:
    """文件出口（轮转）。建目录 / 开文件失败时返回 None —— 旁路铁律：

    日志写不了不该让程序起不来（只读盘、磁盘满、权限不足都可能发生）。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # delay=True：构造时不立刻开文件，第一次真写才开——避免测试 / 一次性脚本
        # 只 import 就留下空文件。
        handler = logging.handlers.RotatingFileHandler(
            str(path),
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        return None
    handler.setFormatter(_make_formatter())
    return handler


def configure_logging(
    *,
    level: str | None = None,
    log_file: str | Path | None = None,
) -> logging.Logger:
    """把 `glyph` 父 logger 接到控制台 + 文件；**幂等**。

    参数留 None 表示"读环境变量"（`GLYPH_LOG_LEVEL` / `GLYPH_LOG_FILE`），
    显式传值优先——测试与装配根都走显式传参，不依赖环境。

    **幂等**：重复调用先摘掉上次自己挂的 handler 再挂。设置页改配置会重建 runtime，
    不做幂等会看到同一条日志打三遍。

    **不碰 root、不碰 uvicorn**：只配置 `glyph` 父 logger，且 `propagate=False`
    —— 否则消息会再冒泡到 root，被 `lastResort` 重复打一遍（无格式的那种）。

    只用标准库，零新依赖（红线：不擅自引入依赖）。
    """
    raw_level = level if level is not None else os.environ.get(LOG_LEVEL_ENV, "")
    raw_file = log_file if log_file is not None else os.environ.get(LOG_FILE_ENV)

    logger = logging.getLogger(_GLYPH_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - 关旧 handler 失败不该影响重新配置
            pass

    logger.setLevel(normalize_level(raw_level))
    logger.propagate = False

    logger.addHandler(_console_handler())

    # 空串 = 显式关掉文件出口；None = 没配过，用默认路径
    if raw_file is None:
        file_path: Path | None = default_log_file()
    else:
        text = str(raw_file).strip()
        file_path = Path(text).expanduser() if text else None
    if file_path is not None:
        handler = _file_handler(file_path)
        if handler is not None:
            logger.addHandler(handler)

    # 第三方库固定压在 WARNING：不跟随 GLYPH_LOG_LEVEL=DEBUG（可能打印鉴权头）。
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(max(_THIRD_PARTY_LEVEL, logger.level))

    return logger
