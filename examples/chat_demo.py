"""最小真实对话演示：在终端里亲眼看着 agent 边想边调工具。

这不是产品功能，而是第一层运行时（ToolRegistry + 流式 AgentLoop + 千问适配）的
手动联调脚本，让你不写测试代码，也能用真实 key 端到端跑一次，看见：
  - 模型的回复一个字一个字"流"出来；
  - 模型自己决定要不要调工具、调哪个；
  - 工具结果回灌后，模型接着把话说完；
  - 结束时给出终态原因、轮次和 token 用量。

交互式运行（在 markr/ 目录下）：
    .venv\\Scripts\\python examples\\chat_demo.py
然后打字回车即可多轮对话，输入 quit / exit 退出。

只跑一句（非交互，便于快速验证）：
    .venv\\Scripts\\python examples\\chat_demo.py "帮我算一下 (123+456)*2"

前提：markr/.env 里配置了 DASHSCOPE_API_KEY（参考 .env.example）。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from pathlib import Path

# 不做 editable 安装（原因见 AGENTS.md 的 R9），直接把 src 加进导入路径
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

# Windows 中文控制台默认 GBK，统一成 UTF-8，避免打印中文 / 特殊符号时崩
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import openai  # noqa: E402

from glyph.config import load_settings  # noqa: E402
from glyph.llm.qwen import QwenLLM  # noqa: E402
from glyph.runtime.loop import AgentLoop  # noqa: E402
from glyph.runtime.registry import ToolRegistry  # noqa: E402
from glyph.runtime.tool import Tool  # noqa: E402
from glyph.session import SessionHistory, new_session  # noqa: E402


class CalculatorTool(Tool):
    """一个模型会主动用的工具：精确算数，避免它自己心算出错。"""

    name = "calculator"
    description = (
        "当需要做任何数学计算（加减乘除、取模、幂、括号组合）时使用本工具，不要自己心算，以免算错。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如 (123+456)*2 或 2**10",
            }
        },
        "required": ["expression"],
    }

    # 白名单：只允许数字和四则运算符，禁止任何名字 / 函数调用 / 属性访问
    _ALLOWED_NODES = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.USub,
        ast.UAdd,
    )

    async def run(self, args: dict) -> dict:
        expression = args["expression"]
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, self._ALLOWED_NODES):
                raise ValueError("表达式中存在不允许的内容")
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
                raise ValueError("只允许数字常量")
        # 内置函数全部清空，双保险，杜绝任意代码执行
        value = eval(compile(tree, "<calculator>", "eval"), {"__builtins__": {}}, {})
        return {"expression": expression, "result": value}


class WordCountTool(Tool):
    """一个贴近英语学习场景的小工具：统计单词 / 字符 / 句子数。"""

    name = "word_count"
    description = "统计一段英文文本的单词数、字符数和句子数（按空格分词，按 . ! ? 数句子）。"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要统计的英文文本"}},
        "required": ["text"],
    }

    async def run(self, args: dict) -> dict:
        text = args["text"]
        return {
            "words": len(text.split()),
            "characters": len(text),
            "sentences": sum(1 for ch in text if ch in ".!?"),
        }


SYSTEM_PROMPT = (
    "你是 Glyph，一个乐于助人的英语学习助手。"
    "遇到数学计算或文本统计，请调用提供的工具完成，不要自己心算。"
    "用简洁的中文与用户交流，引用英文时直接给出英文。"
)


async def build_session() -> tuple[AgentLoop, SessionHistory]:
    """装配一次会话：模型 + 工具注册表 + 落盘的会话历史，再把历史注入主循环。"""
    settings = load_settings()
    if not settings.api_key:
        print("没读到 DASHSCOPE_API_KEY，请先在 markr/.env 里配置（可参考 .env.example）。")
        sys.exit(1)

    # client 读取超时要宽于主循环的流式空闲超时（默认 90s），让应用层先按"卡住"自救、
    # SDK 超时只作更外层兜底
    client = openai.AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=120.0,
    )
    llm = QwenLLM(settings, client=client)

    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(WordCountTool())

    # 会话历史负责落盘与上下文重建（默认写到 ~/.markr/sessions）；loop 只用它、不管它怎么存
    history = await new_session(system_prompt=SYSTEM_PROMPT, model=settings.model, cwd=os.getcwd())
    loop = AgentLoop(
        llm,
        registry,
        history=history,
        max_output_tokens=settings.max_tokens,
        max_output_tokens_upgrade=settings.max_tokens_upgrade,
        stream_idle_timeout=settings.stream_idle_timeout,
        tool_timeout=settings.tool_timeout,
    )
    return loop, history


async def run_once(loop: AgentLoop, user_input: str) -> None:
    """跑一轮，把事件流实时画到终端。"""
    inline = False  # 当前是否有还没换行的正文（打印工具行前先补一个换行）

    async for event in loop.run(user_input):
        kind = event.type

        if kind == "text_delta":
            # 正文：逐字上屏
            sys.stdout.write(event.delta)
            inline = not event.delta.endswith("\n")

        elif kind == "tool_call":
            # 工具宣告：模型决定调工具、参数已完整，马上执行
            if inline:
                print()
                inline = False
            args = json.dumps(event.call.arguments, ensure_ascii=False)
            print(f"  [工具] 调用 {event.call.name}({args})")

        elif kind == "tool_result":
            # 工具执行结束（成功 / 失败都显示），随后模型会看到结果继续说
            if event.ok:
                body = json.dumps(event.output, ensure_ascii=False)
                print(f"  [工具结果] {body}  ({event.duration_ms:.0f} ms)")
            else:
                print(f"  [工具失败] {event.error}  ({event.duration_ms:.0f} ms)")

        elif kind == "turn_retry":
            # 网络断了重发、或输出撞上限静默升档：本轮重来，提示一行
            print(f"  [重试] {event.message}")

        elif kind == "result":
            # 一轮结束：终态原因 / 迭代轮数 / token
            if inline:
                print()
            usage = event.usage
            token_info = ""
            if usage is not None:
                token_info = (
                    f" · 输入 {usage.input_tokens or 0} / 输出 {usage.output_tokens or 0} tokens"
                )
            print(f"  -- 完成（{event.finish_reason.value} · {event.iterations} 轮{token_info}）")

        elif kind == "error":
            if inline:
                print()
                inline = False
            print(f"  [错误] {event.message}")


async def interactive(loop: AgentLoop) -> None:
    print("Glyph 真实对话测试。直接打字回车即可多轮对话，输入 quit 或 exit 退出。")
    print("可以试试：")
    print("  - 帮我算一下 (123+456)*2 是多少")
    print("  - 数一下这句话有几个单词：I really enjoy learning English every day.")
    print("  - 你好，你是谁？（这题没有工具，看它直接回答）")
    print()

    while True:
        try:
            # 阻塞的 input() 放到线程里，避免卡住事件循环（ASYNC250）；本地单用户 demo 够用
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
        # 带命令行参数 = 非交互单次模式（便于快速验证 / 截图）
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
