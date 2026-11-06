"""日志接线（observability）的离线测试。

守的是 docs/design/realtime-logging-design.md 里定下的几条硬规矩：

1. **接线幂等**：同一个进程里重复调用 configure_logging 不能越挂越多 handler
   （设置页改配置会重建 runtime，重复挂会看到同一条日志打三遍）；
2. **级别生效**：GLYPH_LOG_LEVEL 决定过滤门槛，非法值静默回退 INFO（不炸启动）；
3. **脱敏从机制上保证**：参数摘要里字符串只报长度，绝不把作文原文写进日志；
4. **不碰 root / 不碰 uvicorn**：只配置 markr 父 logger，且 propagate=False
   （防冒泡到 root 被 lastResort 重复打一遍）；
5. **第三方库压到 WARNING**：httpx / openai 的 DEBUG 会把请求细节（含鉴权头）打出来，
   必须钉死，绝不跟随 GLYPH_LOG_LEVEL=DEBUG；
6. **显式 UTF-8 + 中文不炸**：控制台被重定向时 locale 编码不可靠（R9 的坑类）；
7. **import 无副作用**：不 import 就不该有任何 handler 被挂上（现有 1800+ 测试
   的断言不依赖 logger，但行为必须与现在一致）。
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from glyph.observability import (
    THIRD_PARTY_LOGGERS,
    configure_logging,
    summarize_args,
)


# 每个用例跑完把自己挂上的 handler 摘干净，避免污染同一进程里的其他测试
@pytest.fixture(autouse=True)
def _clean_markr_logger():
    root = logging.getLogger("glyph")
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
    root.setLevel(logging.NOTSET)


# --------------------------------------------------------------- 脱敏摘要


def test_summarize_args_string_reports_length_only() -> None:
    """字符串参数只报长度——这是"作文原文不进日志"的机制保证。"""
    out = summarize_args({"text": "My favourite subject is English."})
    assert "My favourite" not in out
    assert "32字" in out


def test_summarize_args_scalars_report_values() -> None:
    """数字 / 布尔直接报值：定位问题时这些值本身没有隐私风险。"""
    out = summarize_args({"max_rounds": 2, "urgent": True})
    assert "max_rounds=2" in out
    assert "urgent=True" in out


def test_summarize_args_truncates_long_result() -> None:
    """参数很多时截断，别让一行日志变成一屏。"""
    out = summarize_args({f"k{i}": i for i in range(50)})
    assert len(out) < 300
    assert "另42个" in out  # 50 - 8（保留的前 8 个）


def test_summarize_args_handles_empty_and_none() -> None:
    assert summarize_args({}) == ""
    assert summarize_args(None) == ""


def test_summarize_args_never_leaks_api_key_like_string() -> None:
    """哪怕调用方把 key 当参数传进来，也只该看到长度。"""
    out = summarize_args({"api_key": "sk-abcdef1234567890"})
    assert "sk-abcdef" not in out
    assert "19字" in out


# --------------------------------------------------------------- 接线


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """重复调用不叠加 handler（设置页重建 runtime 会重复调）。"""
    log_file = tmp_path / "backend.log"
    configure_logging(level="INFO", log_file=str(log_file))
    first = len(logging.getLogger("glyph").handlers)
    configure_logging(level="INFO", log_file=str(log_file))
    second = len(logging.getLogger("glyph").handlers)
    assert first == second
    assert first > 0


def test_configure_logging_sets_no_propagate() -> None:
    """propagate=False：防止再冒泡到 root 被 lastResort 重复打一遍。"""
    configure_logging(level="INFO", log_file="")
    assert logging.getLogger("glyph").propagate is False


def test_configure_logging_never_touches_root(tmp_path: Path) -> None:
    """只配置 markr 父 logger；root 上不许多出我们挂的 handler。"""
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging(level="INFO", log_file=str(tmp_path / "b.log"))
    assert list(root.handlers) == before


def test_level_is_applied(tmp_path: Path) -> None:
    configure_logging(level="WARNING", log_file=str(tmp_path / "b.log"))
    assert logging.getLogger("glyph").level == logging.WARNING


@pytest.mark.parametrize("bad", ["", "  ", "NOT_A_LEVEL", "好", "123"])
def test_invalid_level_falls_back_to_info(bad: str, tmp_path: Path) -> None:
    """非法级别静默回退 INFO —— 不能因为配错一个字就起不来（与其他配置容错惯例一致）。"""
    configure_logging(level=bad, log_file=str(tmp_path / "b.log"))
    assert logging.getLogger("glyph").level == logging.INFO


def test_level_is_case_insensitive(tmp_path: Path) -> None:
    configure_logging(level="debug", log_file=str(tmp_path / "b.log"))
    assert logging.getLogger("glyph").level == logging.DEBUG


# --------------------------------------------------------------- 出口


def test_writes_to_file_in_utf8(tmp_path: Path) -> None:
    """文件出口：显式 UTF-8，中文能读回来（R9 的坑类）。"""
    log_file = tmp_path / "backend.log"
    configure_logging(level="INFO", log_file=str(log_file))
    logging.getLogger("glyph.test").info("中文也要能写进去 ok=%s", True)
    for handler in logging.getLogger("glyph").handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "中文也要能写进去" in text
    assert "ok=True" in text


def test_file_line_has_time_level_and_source(tmp_path: Path) -> None:
    """格式统一为「时间 级别 [来源] 消息」，才能 grep。"""
    log_file = tmp_path / "backend.log"
    configure_logging(level="INFO", log_file=str(log_file))
    logging.getLogger("glyph.runtime").info("轮结束 iter=1")
    for handler in logging.getLogger("glyph").handlers:
        handler.flush()
    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "INFO" in line
    assert "glyph.runtime" in line
    assert "轮结束 iter=1" in line
    # 开头是 HH:MM:SS
    assert line[:2].isdigit() and line[2] == ":"


def test_empty_log_file_disables_file_output(capsys) -> None:
    """显式传空串 = 关文件出口，只留控制台。"""
    configure_logging(level="INFO", log_file="")
    handlers = logging.getLogger("glyph").handlers
    assert handlers, "控制台出口应该还在"
    assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers)


def test_file_handler_is_rotating_and_bounded(tmp_path: Path) -> None:
    """文件必须轮转且有上限：否则跑久了能把硬盘写满。"""
    log_file = tmp_path / "backend.log"
    configure_logging(level="INFO", log_file=str(log_file))
    rotating = [
        h
        for h in logging.getLogger("glyph").handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].maxBytes > 0
    assert rotating[0].backupCount > 0


# --------------------------------------------------------------- 第三方库安全


@pytest.mark.parametrize("name", sorted(THIRD_PARTY_LOGGERS))
def test_third_party_loggers_pinned_to_warning(name: str) -> None:
    """这些库的 DEBUG 会打 HTTP 请求细节（含鉴权头），必须永远压在 WARNING。"""
    configure_logging(level="DEBUG", log_file="")
    assert logging.getLogger(name).level >= logging.WARNING


def test_third_party_not_raised_even_when_debug(tmp_path: Path) -> None:
    """反证：把主级别开到 DEBUG，第三方也不能跟着放开。"""
    configure_logging(level="DEBUG", log_file=str(tmp_path / "b.log"))
    for name in THIRD_PARTY_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
