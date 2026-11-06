"""真机走查：模型到底会不会滥用"问一句"（后端 ask_user 的 V1–V4 验收）。

用法：.venv/Scripts/python.exe scripts/verify_ask_user_real.py
注意：**会真调模型烧额度**（用 .env 的 key，主脑 deepseek-v4.1-flash）。

验什么（按重要性排）：
- **V1 不该问别问**：需求已经很明确时，它必须直接干活，不能拿芝麻大的事来烦人。
  这条挂了 = 这个功能是负资产（每句话都弹卡片，用起来烦死人）。
- **V2 该问要问**：需求真的有分叉、猜不出来时，它得张嘴。
- **V3 选项质量**：2–4 个、互斥、有没有自己造"其他"（那是界面自动补的，模型不许造）。
- **V4 拿到答案要接着办**：不能答完又问一遍、也不能空转。

怎么让"用户"自动回答：监听事件流里的 ``tool_call``（name == ask_user），
参数里就有 question 和 options —— 顺带验了前端那条契约（卡片要用的东西都在这帧里）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

import openai  # noqa: E402

from glyph.config import load_settings  # noqa: E402
from glyph.llm.responses import ResponsesLLM  # noqa: E402
from glyph.runtime.loop import AgentLoop  # noqa: E402
from glyph.runtime.registry import ToolRegistry  # noqa: E402
from glyph.session import new_session  # noqa: E402
from glyph.tools.ask_user import AskUserGateway  # noqa: E402
from glyph.tools.assemble import build_default_registry  # noqa: E402
from glyph.tools.dict_repo import DictRepository, default_dict_path  # noqa: E402

# 第一组：需求够明确，模型该直接干活（滥用检测）
EASY: list[tuple[str, str, bool]] = [
    ("V1-查词（很明确）", "reluctant 这个词是什么意思？", False),
    ("V1-批改（很明确）", "帮我批改这句话：He go to school yesterday.", False),
]

# 第三组：**缺素材**（只说要干嘛、内容一个字没给）。
# 这种不该用卡片 —— 问"你要翻译什么"列不出 2–4 个选项，回一句话把东西要过来才对。
# 所以期望值是"不调"；调了反而是滥用（弹一张空卡片把人卡住）。
NO_MATERIAL: list[tuple[str, str, bool]] = [
    ("V3-缺素材-翻译", "帮我翻译一下", False),
    ("V3-缺素材-改", "把这个也改一下", False),
]

# 第四组：真正的验收场景 —— **发了素材却一句话没说**，且素材本身没有硬错。
# 转写 / 批改 / 润色 / 翻译 / 只是看看，几个分支做法差太远且都列得出来，
# 这是该弹卡片的地方（提示词里也明确写了"必须用 ask_user"）。
SHOULD_ASK: list[tuple[str, str, bool]] = [
    ("V4-发素材不说话（无硬错）", "The meeting is scheduled for tomorrow morning.", True),
]

# 第五组：同样"发素材不说话"，但素材**有硬错**。
# 期望值是**不问**：提示词里"改已有的英文 → 必须调用 grade_text"已经是硬路由，
# 这时候再弹一张卡片问"要不要批改"是多此一举，直接批改才是好的默认。
# （第一轮我把这条标成"该问"，是真机跑完才想明白：错的是期望值，不是模型。）
HAS_HARD_ERROR: list[tuple[str, str, bool]] = [
    ("V5-发素材不说话但明显有错", "I very like this book.", False),
]

CASES = EASY + NO_MATERIAL + SHOULD_ASK + HAS_HARD_ERROR

ANSWER_DELAY = 0.6  # 假装用户读题的时间
PER_CASE_TIMEOUT = 120.0

# 真机走查会烧额度：一旦撞上额度/鉴权失败，后面每个场景都会"零工具调用"，
# 若不识别就会被记成"该问却没问"——拿一次跑空的错误当模型的判断，会改错提示词。
_quota_dead = False
_QUOTA_HINTS = ("额度", "API Key", "quota", "Free quota", "模型调用失败", "暂时用不了")


def _is_quota_error(text: str) -> bool:
    return any(h in text for h in _QUOTA_HINTS)


class ImageStub:
    """看图工具要的图源：本脚本不发图，任何取图都当没有。"""

    async def read(self, image_id: str) -> tuple[bytes, str]:
        return b"", "application/octet-stream"


async def _answer_later(gw: AskUserGateway, question: str, options: list[dict]) -> None:
    """假装用户：读一会儿题，然后点第一个选项（顺便带上 question 做对账）。"""
    await asyncio.sleep(ANSWER_DELAY)
    label = (options[0].get("label") if options else "") or ""
    ok = gw.answer(choice=label, question=question, text="")
    print(f"      🧑 用户点了：{label!r}（对账{'通过' if ok else '被拒'}）")


async def run_case(
    llm, registry, gw, system_prompt, root: Path, title: str, msg: str, should_ask: bool, model: str
) -> dict:
    history = await new_session(system_prompt=system_prompt, model=model, cwd=str(root))
    loop = AgentLoop(
        llm,
        registry,
        history=history,
        max_output_tokens=4096,
        stream_idle_timeout=60.0,
        tool_timeout=60.0,
    )

    asked: list[dict] = []
    tools_used: list[str] = []
    final_text = ""

    async def consume() -> None:
        nonlocal final_text
        async for ev in loop.run(msg):
            t = ev.type
            if t == "tool_call":
                tools_used.append(ev.call.name)
                if ev.call.name == "ask_user":
                    args = ev.call.arguments or {}
                    asked.append(args)
                    opts = args.get("options") or []
                    print(f"      ❓ 问了：{args.get('question')!r}")
                    for o in opts:
                        print(f"           · {o.get('label')} —— {o.get('description')}")
                    asyncio.ensure_future(
                        _answer_later(gw, str(args.get("question") or ""), opts)
                    )
            elif t == "text_delta":
                final_text += ev.delta
            elif t == "result":
                final_text = ev.text or final_text

    await asyncio.wait_for(consume(), timeout=PER_CASE_TIMEOUT)

    return {"title": title, "msg": msg, "should_ask": should_ask, "asked": asked,
            "tools": tools_used, "final": final_text.strip()}


def build_runtime() -> tuple[ResponsesLLM, ToolRegistry, AskUserGateway, str, Path]:
    settings = load_settings()
    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=120.0)
    llm = ResponsesLLM(settings, client=client, web_search=False)
    gw = AskUserGateway()
    tmp = Path(tempfile.mkdtemp(prefix="markr-askuser-"))

    registry, _mem, system_prompt = build_default_registry(
        settings,
        image_store=ImageStub(),
        dict_repo=DictRepository(default_dict_path()),
        upload_dirs=[tmp],
        session_id_provider=lambda: "verify-ask-user",
        history_provider=lambda: None,
        ask_gateway=gw,
    )
    assert isinstance(registry, ToolRegistry)
    print(f"装配完成：ask_user 已注册 = {registry.get('ask_user') is not None}，"
          f"同一通道 = {registry.get('ask_user').gateway is gw}\n")
    return llm, registry, gw, system_prompt, tmp


async def main() -> int:
    global _quota_dead
    # ⚠️ 必须整个脚本共用**一条**事件循环：openai 客户端（以及它底层的连接池）
    # 绑在首次创建它的那条 loop 上。若每个场景各跑一次 asyncio.run，第二个场景起
    # 就会报 "Event loop is closed" —— 那是脚本的锅，不是模型的行为，别当成验收结果。
    llm, registry, gw, system_prompt, tmp = build_runtime()

    results = []
    for title, msg, should_ask in CASES:
        print(f"▶ {title}\n   用户说：{msg}")
        # 额度耗尽 / 鉴权失败时模型根本没说话，那一轮**没有行为可判**。
        # 不挡掉它会被记成"该问却没问"，拿一次跑空的额度错误当成模型的判断去改提示词。
        if _quota_dead:
            print("   ⛔ 前面已出现额度/鉴权失败，本场景跳过（结果不可信）\n")
            results.append(
                {"title": title, "should_ask": should_ask, "asked": [], "tools": [],
                 "final": "", "skipped": True}
            )
            continue
        try:
            r = await run_case(
                llm, registry, gw, system_prompt, tmp, title, msg, should_ask, llm._settings.model
            )
        except TimeoutError:
            print("   ⏱ 超时（这一轮没在 120 秒内收尾）\n")
            results.append(
                {"title": title, "should_ask": should_ask, "asked": [], "tools": [],
                 "final": "", "timeout": True}
            )
            continue
        except Exception as exc:  # noqa: BLE001 - 走查脚本：单个场景炸了也要看后面的
            print(f"   💥 这一场景没跑成：{type(exc).__name__}: {exc}\n")
            results.append(
                {"title": title, "should_ask": should_ask, "asked": [], "tools": [],
                 "final": "", "error": str(exc)}
            )
            continue
        if _is_quota_error(r["final"]):
            _quota_dead = True
            print("   ⛔ 模型调用失败（额度/鉴权）——本场景及之后的结果都不可信\n")
            results.append({**r, "quota_dead": True})
            continue
        print(f"   用了工具：{r['tools'] or '（一个都没调）'}")
        print(f"   最后回复：{(r['final'] or '（空）')[:160]}")
        results.append(r)
        print()

    # ---------- 判定 ----------
    print("=" * 60)
    bad: list[str] = []
    for r in results:
        did_ask = bool(r["asked"])
        want = r["should_ask"]
        if r.get("timeout"):
            bad.append(f"{r['title']}：超时未收尾")
            print(f"❌ {r['title']}：超时")
            continue
        if r.get("quota_dead") or r.get("skipped"):
            # 不算模型不达标，但整个走查作废——没有跑完的验收不能当证据用
            bad.append(f"{r['title']}：没跑成（额度/鉴权），本轮走查作废")
            print(f"⛔ {r['title']}：没跑成（额度/鉴权）")
            continue
        if r.get("error"):
            bad.append(f"{r['title']}：脚本没跑成（{r['error'][:40]}）")
            print(f"💥 {r['title']}：脚本报错")
            continue
        if want and not did_ask:
            bad.append(f"{r['title']}：该问却没问（猜着做了）")
            print(f"❌ {r['title']}：该问却没问")
            continue
        if not want and did_ask:
            bad.append(f"{r['title']}：不该问却问了 → 滥用")
            print(f"❌ {r['title']}：不该问却问了（滥用）")
            continue
        print(f"✅ {r['title']}：{'问了' if did_ask else '没问'}（符合预期）")

        for a in r["asked"]:
            opts = a.get("options") or []
            n = len(opts)
            if not 2 <= n <= 4:
                bad.append(f"{r['title']}：选项 {n} 个，超出 2–4")
                print(f"   ⚠ 选项 {n} 个（应 2–4）")
            for o in opts:
                lab = str(o.get("label") or "")
                if "其他" in lab or "自定义" in lab or lab.strip().lower() == "other":
                    bad.append(f"{r['title']}：模型自己造了「{lab}」（那是界面自动补的）")
                    print(f"   ⚠ 模型自己造了「其他」类选项：{lab}")
            if not str(a.get("question") or "").endswith(("？", "?")):
                print(f"   ⚠ 问题没以问号结尾：{a.get('question')!r}")

        if r["asked"] and len(r["final"] or "") < 20:
            bad.append(f"{r['title']}：问完拿到答案却几乎没输出（空转/又问一遍？）")
            print(f"   ⚠ 拿到答案后几乎没输出：{r['final']!r}")

    print("=" * 60)
    if bad:
        print(f"共 {len(bad)} 项不达标：")
        for b in bad:
            print("  -", b)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
