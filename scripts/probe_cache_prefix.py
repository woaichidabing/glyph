"""缓存前缀探针：在**真实请求形状**下量"那块每轮会变的上下文"到底让多少缓存失效。

为什么重做这个探针
------------------
2026-10-12 早先的探针把整段系统提示词当**一条** system 消息、在中间改一个字，
cached_tokens 从 7168 掉到 0，据此得出"前缀一变、后面全废"的结论。真机复验推翻了它：
用户写了新记忆（上下文块确实变了），紧接着那一轮命中率仍有 95%。
差别就在**请求形状**——真实请求是「系统词 + 工具 + 上下文块 + 历史」，
那块会变的东西只占一小部分。所以探针必须照真实形状造。

探针问的六件事
--------------
(1) 冷启动（同前缀第一次）
(2) 紧接着同前缀再发（历史往后长一条）
(3) 上下文块改一行（模拟"记住了一条记忆"）——当前产品的位置
(4) 上下文块整块删掉（最坏情况）
(5) 上下文块挪到历史之后再改一行——位置方案对比：挪了到底有没有用
(6) 把系统词换成一句话（工具不动）——看缓存里到底装了什么

2026-10-12 实测结论（kimi-k3 / Responses 通道，输入约 1.5 万 token）
--------------------------------------------------------------
- (1)~(5) 命中**全都是 14336**（96.4%~97.0%），包括"整块上下文删掉"和"改一行"：
  那块每轮会变的上下文**根本不影响缓存**；
- (6) 把系统词从 16011 字符换成一句话，命中从 14336 掉到 5120（只剩工具那块）：
  **缓存装的就是「系统词 + 工具 schema」这块固定前导**；
- 尾巴（上下文块 + 历史 + 本轮问题）约 400~700 token，**从来不命中**，哪怕内容一模一样；
- 所以「把上下文块挪到历史之后」**没有收益**（实测五档一模一样），这条优化可以否掉；
- 唯一真正能省的：**把这块固定前导砍小**。它占了一次请求的九成以上，且每轮都在发。
- 历史增长同样不享受缓存（真机第 2 轮 30399 输入只命中 28672 = 2×14336）——
  长会话越来越贵是结构性的，压缩 / 劝新开会话的方向是对的。

运行（markr/ 目录）：.venv\\Scripts\\python.exe scripts/probe_cache_prefix.py
成本：每次调用输入约 1.5 万 token，一共 6 次左右。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from glyph.config import load_dotenv, load_settings  # noqa: E402
from glyph.context import env_date_fragment, render_context_message  # noqa: E402
from glyph.llm.base import LLMError, TurnEnd  # noqa: E402
from glyph.llm.responses import ResponsesLLM  # noqa: E402
from glyph.tools.assemble import build_default_registry  # noqa: E402
from glyph.types import AssistantMessage, SystemMessage, TextBlock, UserMessage  # noqa: E402

QUESTION = "只回复两个字：好的。不要调用任何工具。"

_MEM_HEAD = "# 长期记忆目录\n- [mistake] 主谓一致：三单别丢 s\n- [preference] 喜欢先看例句\n"

_SYSTEM_PROMPT = ""
_TOOLS: list = []


def context_text(extra: str = "") -> str:
    """照真实形状渲染每轮那条上下文消息（日期 + 记忆目录），可插一行模拟"记住了一条"。"""
    memory = "<memory_catalog>\n" + _MEM_HEAD + extra + "</memory_catalog>"
    msg = render_context_message([env_date_fragment(), lambda: memory])
    return msg.content if msg is not None else ""


def history() -> list:
    return [
        UserMessage(content="第一轮：随便聊一句"),
        AssistantMessage(blocks=[TextBlock(text="好，你说。")]),
    ]


def with_context_middle(extra: str) -> list:
    """当前产品的位置：系统词 → 上下文块 → 历史。"""
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        UserMessage(content=context_text(extra)),
        *history(),
        UserMessage(content=QUESTION),
    ]


def with_context_last(extra: str) -> list:
    """对照位置：上下文块挪到历史之后、新问题之前。"""
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        *history(),
        UserMessage(content=context_text(extra)),
        UserMessage(content=QUESTION),
    ]


def without_context() -> list:
    """最坏情况：整块上下文删掉。"""
    return [SystemMessage(content=_SYSTEM_PROMPT), *history(), UserMessage(content=QUESTION)]


def with_short_system() -> list:
    """对照：把系统词换成一句话（工具不动）——用来看清缓存里到底装了什么。"""
    return [
        SystemMessage(content="你是英语学习助手。"),
        UserMessage(content=context_text("")),
        *history(),
        UserMessage(content=QUESTION),
    ]


async def measure(llm: ResponsesLLM, model: str, label: str, messages: list) -> dict:
    """发一次真实请求，取回 usage（含 cached_tokens）。"""
    usage = None
    try:
        async for ev in llm.stream_chat(messages, _TOOLS, max_tokens=16, model=model):
            if isinstance(ev, TurnEnd):
                usage = ev.usage
    except LLMError as exc:
        print(f"  {label:<22} 调用失败：{str(exc)[:110]}")
        return {}
    if usage is None:
        print(f"  {label:<22} 没拿到 usage")
        return {}
    inp = usage.input_tokens or 0
    cached = usage.cached_tokens
    hit = "未知" if cached is None else f"{(cached / inp * 100 if inp else 0):.1f}%"
    shown = str(cached) if cached is not None else "未知"
    print(f"  {label:<22} 输入={inp:<7} 命中={shown:<7} 命中率={hit}")
    return {"input": inp, "cached": cached}


async def pick_model(llm: ResponsesLLM, candidates: list[str]) -> str:
    """挑一个现在真能用的主脑：旗舰档常有"免费额度耗尽"，探针不能因此全灭。

    用一条**极短、且与后面被测前缀无关**的请求探活，避免把 (1) 的"冷启动"提前焐热。
    """
    probe = [UserMessage(content="只回复：好")]
    for model in candidates:
        try:
            # 64 而不是 8：思考型模型给太小的上限会连终态都拿不到（实测踩过）
            async for ev in llm.stream_chat(probe, [], max_tokens=64, model=model):
                if isinstance(ev, TurnEnd):
                    print(f"探活通过：{model}\n")
                    return model
        except LLMError as exc:
            print(f"探活失败 {model}：{str(exc)[:90]}")
    raise SystemExit("候选模型都不可用，先用真机跑通一次对话再试")


async def main() -> None:
    try:  # Windows 控制台默认 GBK，中文输出会变乱码
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 老版本 Python 没有这个 API，无所谓
        pass
    load_dotenv()
    settings = load_settings()

    global _SYSTEM_PROMPT, _TOOLS
    registry, _mem, _SYSTEM_PROMPT = build_default_registry(
        settings,
        image_store=None,
        dict_repo=None,
        upload_dirs=[],
        session_id_provider=lambda: "probe",
        history_provider=lambda: None,
    )
    _TOOLS = registry.schemas()

    print(f"模型：{settings.model}　通道：Responses（与主脑一致）")
    print(f"系统词 {len(_SYSTEM_PROMPT)} 字符　工具 {len(_TOOLS)} 个 schema\n")

    llm = ResponsesLLM(settings, web_search=False)
    # 候选顺序：配置里的主脑 → 主脑的兜底链（同一网关、同一把 key）→ 便宜备选。
    # 2026-10-12 实测：旗舰档与两个 flash 档都撞"免费额度耗尽"，只有 kimi-k3 / qwen3.8-flash
    # 还能用 —— 探针自带探活就是为了不被这个卡死。
    model = await pick_model(
        llm,
        [settings.model, *settings.main_fallback_models, "kimi-k3", "qwen3.8-flash"],
    )
    print(f"本次探针统一用：{model}（同一个模型的缓存才可比）\n")

    print("(1)(2) 冷启动 / 同前缀再发")
    await measure(llm, model, "(1) 冷启动", with_context_middle(""))
    await measure(llm, model, "(2) 同前缀再发", with_context_middle(""))

    print("\n(3) 上下文块改一行（当前产品位置）")
    await measure(llm, model, "(3) 改一行", with_context_middle("- [fact] 十二月考六级\n"))
    await measure(llm, model, "(3b) 改后同前缀再发", with_context_middle("- [fact] 十二月考六级\n"))

    print("\n(4) 上下文块整块删掉（最坏情况）")
    await measure(llm, model, "(4) 整块删掉", without_context())

    print("\n(5) 挪到历史之后、同样改一行")
    await measure(llm, model, "(5) 挪位置+改一行", with_context_last("- [fact] 十二月考六级\n"))

    print("\n(6) 对照：系统词换成一句话（工具不动）")
    await measure(llm, model, "(6) 短系统词", with_short_system())

    print(
        "\n怎么读这张表：\n"
        "  (1)~(5) 命中几乎不变 → 那块会变的上下文**不影响**缓存，挪位置也没收益；\n"
        "  (6) 命中塌下来 → 说明缓存里装的就是「系统词 + 工具」这块前导；\n"
        "  (1) 相对输入：命中占九成以上，说明一次请求的主体就是这块固定前导 ——\n"
        "  要省钱只有一条路：把它砍小。"
    )


if __name__ == "__main__":
    asyncio.run(main())
