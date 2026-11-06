"""真实联网问答演示：走千问 Responses API 的平台内置 web_search。

这是"联网搜索（阶段 1）"的手动联调脚本，用真实 key 端到端跑一次，亲眼看见：
  - 🔍 模型先联网搜索（in_progress / searching / completed 三态实时打印）；
  - completed 时列出它引用了哪些网页来源（链接）；
  - 答案在搜索结束后一个字一个字"流"出来；
  - 结束时给出终态、token 用量和本次搜索次数（搜索按次计费，与 token 分开）。

这条 demo【不挂任何本地工具】（注册表留空），搜索完全由千问云端的内置
web_search 完成；本地工具（如 grade_text）与联网搜索混用见 web 台装配。

⚠️ 一个容易踩的语义坑（T02 已修）：注册表为空 ⇒ 主循环每轮发给模型的 `tools` 都是
`[]`，但这**不等于**"摘掉工具"。摘工具只在"续写轮 / 触顶总结轮"发生，由
`strip_tools=True` 显式表达；普通轮即使没有本地工具，云端 web_search 也必须照旧声明，
否则这条 demo 根本搜不起来。

交互式运行（在 markr/ 目录下）：
    .venv\\Scripts\\python examples\\responses_chat_demo.py
只跑一句（非交互，便于快速验证）：
    .venv\\Scripts\\python examples\\responses_chat_demo.py "2026下半年六级笔试是哪天？"

前提：markr/.env 里配置了 GLYPH_API_KEY / DASHSCOPE_API_KEY（参考 .env.example）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 不做 editable 安装（原因见 AGENTS.md 的 R9），直接把 src 加进导入路径
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

# Windows 中文控制台默认 GBK，统一成 UTF-8，避免打印中文 / emoji 时崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import openai  # noqa: E402

from glyph.config import load_settings  # noqa: E402
from glyph.llm.responses import ResponsesLLM  # noqa: E402
from glyph.runtime.loop import AgentLoop  # noqa: E402
from glyph.runtime.registry import ToolRegistry  # noqa: E402
from glyph.session import SessionHistory, new_session  # noqa: E402

SYSTEM_PROMPT = (
    "你是 Glyph，一个乐于助人的英语学习助手。需要最新信息时请联网搜索核实，"
    "用简洁的中文回答，并在答案里自然地给出依据来源。"
)


async def build_session() -> tuple[AgentLoop, SessionHistory]:
    """装配一次联网会话：Responses 通道（内置 web_search）+ 空注册表 + 落盘历史。"""
    settings = load_settings()
    if not settings.api_key:
        print("没读到 API Key，请先在 markr/.env 里配置（可参考 .env.example）。")
        sys.exit(1)

    client = openai.AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=120.0,  # 联网多轮检索比纯对话慢，给宽一点
    )
    # 阶段 1：平台 web_search 开，本地工具一个都不挂（空注册表）。
    # 注意这里空注册表 ≠ 关搜索：主循环每轮都会把"有没有本地工具"和"要不要摘掉
    # 全部工具"分开告诉适配层，注册表为空只属于前者，云端搜索照旧声明（T02）。
    llm = ResponsesLLM(settings, client=client, web_search=True)

    history = await new_session(system_prompt=SYSTEM_PROMPT, model=settings.model, cwd=os.getcwd())
    loop = AgentLoop(
        llm,
        ToolRegistry(),
        history=history,
        max_output_tokens=settings.max_tokens,
        max_output_tokens_upgrade=settings.max_tokens_upgrade,
        stream_idle_timeout=settings.stream_idle_timeout,
        tool_timeout=settings.tool_timeout,
    )
    return loop, history


async def run_once(loop: AgentLoop, user_input: str) -> None:
    """跑一轮，把事件流实时画到终端。"""
    inline = False  # 当前是否有还没换行的正文（打印状态行前先补换行）

    async for event in loop.run(user_input):
        kind = event.type

        if kind == "web_search":
            if inline:
                print()
                inline = False
            if event.phase == "in_progress":
                print("  🔍 正在联网搜索…")
            elif event.phase == "searching":
                if event.query:
                    print(f"  🔍 搜索：{event.query}")
            elif event.phase == "completed":
                count = len(event.sources)
                query_info = f"（搜索词：{event.query}）" if event.query else ""
                print(f"  🔍 搜索完成{query_info}，引用 {count} 个来源：")
                for source in event.sources[:5]:
                    title = source.title or "(无标题)"
                    print(f"      · {title}  {source.url}")

        elif kind == "text_delta":
            sys.stdout.write(event.delta)
            inline = not event.delta.endswith("\n")

        elif kind == "turn_retry":
            if inline:
                print()
                inline = False
            print(f"  [重试] {event.message}")

        elif kind == "result":
            if inline:
                print()
            usage = event.usage
            token_info = ""
            if usage is not None:
                token_info = (
                    f" · 输入 {usage.input_tokens or 0} / 输出 {usage.output_tokens or 0} tokens"
                )
            print(
                f"  -- 完成（{event.finish_reason.value} · {event.iterations} 轮"
                f" · 联网搜索 {event.web_search_count} 次{token_info}）"
            )

        elif kind == "error":
            if inline:
                print()
                inline = False
            print(f"  [错误] {event.message}")


async def interactive(loop: AgentLoop) -> None:
    print("Glyph 联网问答测试（Responses + 内置 web_search）。")
    print("直接打字回车即可多轮对话，输入 quit 或 exit 退出。可以试试：")
    print("  - 2026 年下半年英语六级笔试是哪天？")
    print("  - 最近一周有什么和英语学习相关的新闻？")
    print("  - 雅思考试现在的报名费是多少？")
    print()

    while True:
        try:
            user_input = (await asyncio.to_thread(input, "你：")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return

        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit", "退出"}:
            print("再见。")
            return

        print("Glyph：")
        await run_once(loop, user_input)
        print()


async def main() -> None:
    loop, history = await build_session()
    try:
        if len(sys.argv) > 1:
            user_input = " ".join(sys.argv[1:]).strip()
            print(f"你：{user_input}")
            print("Glyph：")
            await run_once(loop, user_input)
            return

        await interactive(loop)
    finally:
        history.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n再见。")
