"""§18 / #17 探针：L3 便宜摘要写手选型 + C2 / C4 口径实测（可复跑）。

背景见 docs/design/context-compaction-design.md §9.2、§18。用 markr/.env 的聚合 key，
走 OpenAI 兼容的 chat/completions 通道（摘要只要一次性结构化文本，不需要流式 / 工具，
故用候选最广的 chat 通道，而非主脑的 Responses 通道）。

2026-10-04 实测结论（同一把 key、compatible-mode/v1）：
- 写手选定 **qwen3.7-flash**：
  * 窗口硬指标达标：官方 context window = 1,000,000（max input 991,808），与主脑同量级，
    §9.3 的材料截断 / 对半砍 / map-reduce 后手大概率永不触发；
  * 教学金标摘要 9/9 要点全中（两篇批改的错误点、定稿全文、雅思 6.5 目标、进行中第三篇都保住），
    固定小节结构稳定（temperature=0）；
  * 最便宜档：华北 2 阶梯价 输入 ≤32k ¥0.2 / 输出 ¥0.8（每百万 token）；
    32k–256k ¥0.6/2.4；256k–1M ¥1.2/4.8。按 L3 一次喂 ~150k 材料、输出 8k 估算，
    一次摘要约 ¥0.1–0.2，且 L3 一辈子极少触发。
- qwen3.8-flash：同为 9/9，但单价贵约 4 倍（¥0.8/2.7）、本次 37s 偏慢、输出偏长（2212），
  不作默认（留作备选）。
- qwen-flash（老牌 2025-07-28）：最快最省（2.6s、¥0.15/1.5 档）但金标 8/9，漏掉 g2 三单，
  "长文不丢点"不达标，不作默认。
- deepseek-v4.1-flash：chat 通道 finish_reason=length、1600 token 空转、正文为空、0/9，不可用；
  deepseek-v4-flash：403 免费额度耗尽；glm-5.3-flash：chat 通道 404 model_not_found
  （与 Responses 通道 Unsupported 互相印证，确认不可用）。

C2（input_tokens 是否含 tools）：同消息不带 tools=63、带 20 个工具 schema=2170，差 2107，
  tools **明确计入** input。→ 用真实回传 usage 记账（#66 last_context_output）天然含 tools；
  markr 全套工具 schema 约 3–4k token，相对 300k 窗口 <1.5%，RESERVE_HARD=12k 已覆盖，
  **无需另加 tools 常数、不必重校 RESERVE_HARD**。

C4（连续两条 user 是否非法）：chat 通道 messages=[user, user] 不报错、正常作答。
  → #65 可取消 §11.2 的 user 合并，续接摘要与 context 注入各为一条 user、trust 天然分段。
  注：主脑走 Responses 通道，角色合法性在 #65/#67 于 Responses 通道再复核一次。

运行：在 markr/ 目录  PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_compaction_summarizer.py
"""

from __future__ import annotations

import json
import time

from openai import OpenAI

from glyph.config import load_dotenv, load_settings

# 候选：(model_id, 官方标称窗口, 价格口径 CNY/每百万 token，华北2)
CANDS: list[tuple[str, str, str]] = [
    ("qwen3.7-flash", "1,000,000", "入0.2/出0.8（≤32k）；0.6/2.4（32k–256k）；1.2/4.8（256k–1M）"),
    ("qwen3.8-flash", "1,000,000", "入0.8/出2.7（不分阶梯）"),
    ("qwen-flash", "约1M（价目分档至256k+）", "入0.15/出1.5（≤128k）；0.6/6（128k–256k）"),
    ("deepseek-v4.1-flash", "待查", "入1/出4（千问平台）— 实测空转不可用"),
    ("deepseek-v4-flash", "待查", "— 实测 403 免费额度耗尽"),
    ("glm-5.3-flash", "—", "— 实测 404 不可达（Responses 通道亦 Unsupported）"),
]

# 教学场景金标会话：两篇批改 + 定稿 + 查词 + 备考目标 + 一篇进行中
TRANSCRIPT = """\
[用户] 帮我改这篇日记：Yesterday I go to park and see three dog. I was very happy.
[助手·批改 grade@g1] 发现 3 处硬伤：1) go→went（过去时）；2) see→saw（过去时）；
3) dog→dogs（three 后复数）；另 park 前习惯加 the。改后：Yesterday I went to the park
and saw three dogs. I was very happy.
[用户] 时态那两处我接受，dog 加 s 也接受，就按你改的定稿。
[助手·定稿 grade@g1] 已定稿：Yesterday I went to the park and saw three dogs. I was very happy.
[用户] 再改这篇：I am very interesting in this movie because it make me happy.
[助手·批改 grade@g2] 2 处：1) interesting→interested（be interested in 固定搭配，
人对物感兴趣用 -ed）；2) make→makes（it 三单）。
[用户] interesting 和 interested 到底怎么分？我总分不清。
[助手·查词讲解] -ed 描述人的感受（I am interested），-ing 描述事物令人怎样（The movie is
interesting）。
[用户] 明白了。对了我下个月要考雅思写作，目标 6.5，你以后讲语法尽量结合雅思作文。
[用户] 我刚写了第三篇还没发你，等下发：Nowadays many people thinks...（先发个开头）
"""

SYS = (
    "你是对话压缩器。把下面这段英语学习助手的多轮会话浓缩成结构化中文摘要，供后续继续辅导。"
    "只依据原文、不编造；必须用以下固定小节，每节简明：\n"
    "## 用户目标与偏好\n## 已定稿成果（逐条含 grade 代号、篇名、错误数、定稿结论）\n"
    "## 进行中 / 未决\n## 关键事实（查过的知识点、用户水平、考试目标）\n"
    "## 不能丢的细节"
)

# 金标要点：命中任一别名即得分
RUBRIC: list[tuple[list[str], str]] = [
    (["went", "go→went", "go->went"], "g1 时态错误 go→went"),
    (["saw"], "g1 see→saw"),
    (["dog"], "g1 dog→dogs 复数"),
    (["went to the park and saw three dogs"], "g1 定稿结论全文"),
    (["interested"], "g2 interesting→interested"),
    (["makes", "make→makes", "make->makes"], "g2 make→makes 三单"),
    (["雅思"], "用户考试目标 雅思"),
    (["6.5"], "目标分数 6.5"),
    (["第三篇", "thinks", "many people"], "进行中的第三篇"),
]


def score(text: str) -> tuple[int, list[str]]:
    hit, miss = 0, []
    for alts, desc in RUBRIC:
        if any(a in text for a in alts):
            hit += 1
        else:
            miss.append(desc)
    return hit, miss


def probe_summarizer(client: OpenAI, model: str) -> dict:
    t0 = time.time()
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYS},
                {"role": "user", "content": "请压缩这段会话：\n\n" + TRANSCRIPT},
            ],
            max_tokens=1600,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    txt = r.choices[0].message.content or ""
    _, miss = score(txt)
    return {
        "model": model,
        "ok": True,
        "secs": round(time.time() - t0, 1),
        "finish": r.choices[0].finish_reason,
        "in": r.usage.prompt_tokens,
        "out": r.usage.completion_tokens,
        "score": f"{score(txt)[0]}/{len(RUBRIC)}",
        "miss": miss,
        "sample": txt[:200].replace("\n", " "),
    }


def probe_tools_counted(client: OpenAI, model: str) -> None:
    """C2：同消息带 / 不带 tools，对比 prompt_tokens 差值。"""
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "说一个字：好"},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{i}",
                "description": "x" * 400,
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
        for i in range(20)
    ]
    p0 = client.chat.completions.create(
        model=model, messages=msgs, max_tokens=8
    ).usage.prompt_tokens
    p1 = client.chat.completions.create(
        model=model, messages=msgs, tools=tools, max_tokens=8
    ).usage.prompt_tokens
    print(
        f"\n[C2] {model}: 不带 tools={p0}，带20个 tools={p1}，"
        f"差={p1 - p0}（>0 即 tools 计入 input）"
    )


def probe_consecutive_user(client: OpenAI, model: str) -> None:
    """C4：连续两条 user 是否被拒。"""
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "第一句"}, {"role": "user", "content": "第二句"}],
            max_tokens=16,
        )
        print(f"[C4] {model}: 连续 user 未报错，finish={r.choices[0].finish_reason}")
    except Exception as exc:  # noqa: BLE001
        print(f"[C4] {model}: 连续 user 报错 -> {type(exc).__name__}: {str(exc)[:200]}")


def main() -> None:
    load_dotenv()
    s = load_settings()
    client = OpenAI(api_key=s.api_key, base_url=s.base_url)

    print("=" * 24, "写手可达性 + 教学金标摘要", "=" * 24)
    results = []
    for model, window, price in CANDS:
        r = probe_summarizer(client, model)
        r["window"], r["price"] = window, price
        results.append(r)
        if r["ok"]:
            print(
                f"\n### {model}  OK {r['secs']}s in={r['in']} out={r['out']} "
                f"finish={r['finish']} 要点={r['score']}\n  窗口={window}\n  价格={price}\n"
                f"  漏点={'；'.join(r['miss']) or '无'}\n  样例={r['sample']}"
            )
        else:
            print(f"\n### {model}  FAIL  {r['error']}")

    ok_model = next((r["model"] for r in results if r["ok"] and r["score"].startswith("9")), None)
    if ok_model:
        print("\n" + "=" * 24, "C2 / C4 口径（用金标满分模型）", "=" * 24)
        probe_tools_counted(client, ok_model)
        probe_consecutive_user(client, ok_model)

    print("\nRAW=" + json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main()
