"""上下文称重报告（**只读**）：把已有会话回放一遍，打印每一轮上下文的成分占比。

为什么需要它：上下文压缩 L0 的最后一步是「看数字」。没有真实曲线，去重 / 瘦身的
阈值只能靠猜（见《上下文压缩设计方案》§4.6 的顺序坑）。

**只读承诺**：不写任何会话文件、不写记忆、不发任何网络请求。可以放心对真实数据跑。

用法：
    .venv/Scripts/python.exe scripts/context_report.py
    .venv/Scripts/python.exe scripts/context_report.py --top 3
    .venv/Scripts/python.exe scripts/context_report.py --no-memory   # 不计记忆目录，看纯对话

两处「估算」要心里有数（都会在输出里标注）：
- **说明书**这一桶按**当前的**系统词算。系统词原文不落盘，只留指纹，
  回放时拿不到当时的那份，量级对得上但可能差几个字。
- **工具清单**不在 ``messages()`` 里（它是请求体的另一个字段），所以**没有计入**。
  实测 11 个工具约 12 KB，看数时记得心里加上。
"""

# ruff: noqa: E402 —— 下面必须先改 sys.path 才能 import glyph：
# 项目禁止 editable 安装（R9，中文路径会崩解释器），靠 pyproject 的 pythonpath=["src"]，
# 而那个只对 pytest 生效，直接跑脚本时 src 不在路径上。

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from glyph.agent_prompt import build_system_prompt
from glyph.config import (
    DEFAULT_MEMORY_CATALOG_MAX_BYTES,
    DEFAULT_MEMORY_CATALOG_MAX_ENTRIES,
    DEFAULT_MEMORY_FACT_UNUSED_DAYS,
    DEFAULT_MEMORY_LINE_MAX_CHARS,
)
from glyph.context.inject import env_date_fragment
from glyph.memory.inject import MemoryCatalogInjector
from glyph.memory.paths import memory_root
from glyph.memory.store import MemoryStore
from glyph.session import open_session
from glyph.session.paths import sessions_root

# 输出对齐用的列宽（数字列右对齐，中文表头宽度按两个西文字符算，故留宽一点）
_NAME_WIDTH = 34
_NUM_WIDTH = 9


def _kb(n: int) -> str:
    return f"{n / 1024:.1f}K"


def _build_context(with_memory: bool) -> list:
    """复现每轮注入的那几块：日期 + 记忆目录（背词进度要前端数据，脚本里没有）。"""
    fragments = [env_date_fragment()]
    if with_memory:
        injector = MemoryCatalogInjector(
            MemoryStore(memory_root()),
            max_entries=DEFAULT_MEMORY_CATALOG_MAX_ENTRIES,
            fact_unused_days=DEFAULT_MEMORY_FACT_UNUSED_DAYS,
            line_max_chars=DEFAULT_MEMORY_LINE_MAX_CHARS,
            whole_max_bytes=DEFAULT_MEMORY_CATALOG_MAX_BYTES,
        )
        fragments.append(injector.fragment)
    return fragments


async def _weigh(session_id: str, system_prompt: str, context: list) -> tuple:
    history = await open_session(session_id, system_prompt=system_prompt, context=context)
    try:
        stats = history.context_stats()
        return stats, len(history.messages())
    finally:
        history.close()


def _print_table(rows: list) -> None:
    header = "会话".ljust(_NAME_WIDTH) + "".join(
        h.rjust(_NUM_WIDTH)
        for h in ("条数", "合计", "说明书", "注入块", "用户", "助手", "参数", "成品", "脚手架")
    )
    print(header)
    print("-" * len(header))
    for name, count, s in rows:
        print(
            name.ljust(_NAME_WIDTH)
            + str(count).rjust(_NUM_WIDTH)
            + _kb(s.total_bytes).rjust(_NUM_WIDTH)
            + _kb(s.system_bytes).rjust(_NUM_WIDTH)
            + _kb(s.context_bytes).rjust(_NUM_WIDTH)
            + _kb(s.user_bytes).rjust(_NUM_WIDTH)
            + _kb(s.assistant_bytes).rjust(_NUM_WIDTH)
            + _kb(s.tool_call_bytes).rjust(_NUM_WIDTH)
            + _kb(s.artifact_bytes).rjust(_NUM_WIDTH)
            + _kb(s.scaffolding_bytes).rjust(_NUM_WIDTH)
        )


def _print_breakdown(name: str, stats) -> None:
    print(f"\n【{name}】各成分占比")
    parts = stats.parts()
    if stats.total_bytes == 0:
        print("  （这个会话还没有内容）")
        return
    for label, value in parts.items():
        ratio = value / stats.total_bytes * 100
        bar = "█" * max(1, int(ratio / 3)) if value else ""
        print(f"  {label:<10}{_kb(value):>8}  {ratio:5.1f}%  {bar}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="上下文称重报告（只读）")
    parser.add_argument("--top", type=int, default=5, help="最多看几个会话（按最近修改）")
    parser.add_argument("--no-memory", action="store_true", help="不计记忆目录")
    args = parser.parse_args()

    root = sessions_root()
    files = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"没找到任何会话：{root}")
        return 1
    files = files[: args.top]

    system_prompt = build_system_prompt()
    context = _build_context(not args.no_memory)

    print(f"会话根目录：{root}")
    print(f"共 {len(files)} 个会话（按最近修改排序），单位 KB，1K=1024 字节")
    print("注：说明书按当前系统词估算；工具清单（约 12K）不在 messages() 里，未计入")

    rows = []
    for path in files:
        session_id = path.stem
        try:
            stats, count = await _weigh(session_id, system_prompt, context)
        except Exception as exc:  # 坏文件不该让整份报告泡汤
            print(f"\n  [{session_id}] 读不出来，跳过：{type(exc).__name__}: {exc}")
            continue
        rows.append((session_id, count, stats))

    print()
    _print_table(rows)

    if rows:
        newest_name, _, newest = rows[0]
        _print_breakdown(newest_name, newest)

        empty = next((r for r in rows if r[1] <= 2), None)
        if empty is not None:
            print(
                f"\n【固定开销基线】{empty[0]} 只有 {empty[1]} 条消息，"
                f"合计 {_kb(empty[2].total_bytes)} —— 这就是什么都没聊时每轮也要发的量。"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
