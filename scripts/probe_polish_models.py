"""探针：润色升档（polish_text）的子模型选型 —— 可达性 + 改得好不好 + 是否老实说"没得改"。

对应 polish 设计文档 §11.1 P5（"润色模型选型未实测"）。照
``scripts/probe_compaction_summarizer.py`` 的范式写：同一把 key、同一个兼容端点。

⚠️ 为什么每个模型起一个子进程跑：模型不可达时（额度耗尽 / 名字不存在）openai 客户端会
自己重试到很长的超时，串行跑 17 个候选能把整轮卡死。子进程 + 硬超时 + 逐行刷盘，
让"卡住的那个"被单独掐掉，其余照跑。

跑法：在 markr/ 目录
    PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_polish_models.py
    PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_polish_models.py --one <model>
    PYTHONPATH=src .venv/Scripts/python.exe scripts/probe_polish_models.py --full
注意：会真的调模型（用 .env 里的 key），有少量费用。
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from glyph.config import load_dotenv, load_settings

# 候选池：项目已有实测记录的型号 + 常见免费/轻量档。
CANDS: tuple[str, ...] = (
    "qwen3.8-max",
    "deepseek-v4.1-flash",
    "qwen3.5-flash",
    "qwen3.7-flash",
    "qwen3.8-flash",
    "qwen3.8-27b",
    "kimi-k3",
    "glm-5.2",
    "glm-5.3",
    "qwen-flash",
    "qwen3.7-plus",
    "qwen3.5-plus",
    "qwen-max",
    "qwen-plus",
    "qwen-turbo",
)

SYS = """你是英语表达升档助手。用户会给你一段英文，你只做"改得更好"，不纠正语法错误。

铁律：
1. 升档 = 词更准 / 句子更紧 / 语域更匹配。**不是换更难更生僻的词**。
2. 最多挑 5 处。每处给两个版本：safe（稳，保证不出错）、advanced（往上一级靠）。
3. **如果这段英文已经写得很好，就必须老实说没得改，不要为了显得有用硬挑。**
4. 只挑句子给出改法，**不要给整篇润色版**。

只输出 JSON，不要任何其他文字：
{
  "suggestions": [
    {"original": "原句（必须是原文里逐字出现的完整句子）",
     "issue": "为什么值得改（简体中文，一句话）",
     "issue_kind": "word_precision | sentence_tightness | register",
     "safe": "稳妥版",
     "advanced": "进阶版",
     "why": "为什么这样改更好（简体中文）"}
  ],
  "nothing_to_polish": false,
  "overall": "一句话总评（简体中文）"
}
若没得改：suggestions 为 []，nothing_to_polish 为 true，overall 说明它已经好在哪里。"""

CASE_FLAT = (
    "I think this is a very good idea. "
    "We should do it because it can make our life better. "
    "Many people think so too."
)
CASE_GOOD = (
    "The committee rejected the proposal, not because the numbers were wrong, "
    "but because nobody could say who would carry the cost. "
    "That silence, more than any spreadsheet, decided the vote."
)
CASE_HARD = (
    "With the development of society, more and more people attach importance to "
    "the protection of environment. As we all know, environment is very important "
    "for our health. So we must pay attention to this problem."
)
CASES: tuple[tuple[str, str, str], ...] = (
    ("FLAT", CASE_FLAT, "平淡但没错 → 该改，且不靠换难词"),
    ("GOOD", CASE_GOOD, "本来就写得好 → 必须老实说没得改（V4 最关键）"),
    ("HARD", CASE_HARD, "中式英语 → 该改，禁堆难词"),
)


def _strip_fence(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw[3:]
        raw = raw.removeprefix("json").strip()
        if raw.endswith("```"):
            raw = raw[:-3]
    return raw.strip()


def call(client: OpenAI, model: str, text: str, timeout: float) -> dict:
    t0 = time.time()
    try:
        r = client.with_options(timeout=timeout).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYS},
                {"role": "user", "content": "请升档这段英文：\n\n" + text},
            ],
            max_tokens=2000,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}

    txt = r.choices[0].message.content or ""
    raw = _strip_fence(txt)
    try:
        obj = json.loads(raw)
        sug = obj.get("suggestions", [])
        n = len(sug) if isinstance(sug, list) else -1
        nothing = bool(obj.get("nothing_to_polish", False))
        parsed = True
    except Exception as exc:  # noqa: BLE001
        n, nothing, parsed = -1, False, False
        raw = f"{type(exc).__name__}"
    return {
        "ok": True,
        "secs": round(time.time() - t0, 1),
        "finish": r.choices[0].finish_reason,
        "out": r.usage.completion_tokens,
        "json": parsed,
        "n": n,
        "nothing": nothing,
        "sample": _strip_fence(txt)[:260].replace("\n", " "),
        "raw": raw[:200] if not parsed else "",
    }


def one_model(model: str, timeout: float, cases: int) -> None:
    load_dotenv()
    s = load_settings()
    client = OpenAI(api_key=s.api_key, base_url=s.base_url)
    run = CASES[:cases]
    for tag, text, intent in run:
        r = call(client, model, text, timeout)
        if not r["ok"]:
            print(f"[{model}] {tag}  ✗ {r['error']}", flush=True)
            return
        mark = "✓" if r["json"] else "✗JSON"
        print(
            f"[{model}] {tag}  {r['secs']}s finish={r['finish']} out={r['out']} "
            f"{mark} 建议数={r['n']} 没得改={r['nothing']}  ({intent})",
            flush=True,
        )
        if r["json"]:
            print(f"          {r['sample']}", flush=True)
        else:
            print(f"          RAW={r['raw']}", flush=True)


def main() -> None:
    args = sys.argv[1:]
    load_dotenv()
    s = load_settings()
    print(f"端点：{s.base_url}", flush=True)

    if args and args[0] == "--one":
        one_model(args[1], timeout=45.0, cases=3)
        return

    cases = 3 if "--full" in args else 1
    timeout = 45.0
    print(f"候选 {len(CANDS)} 个，每模型 {cases} 个用例，单次超时 {timeout}s\n", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        for m in CANDS:
            ex.submit(one_model, m, timeout, cases)
    print(f"\n全部完成，用时 {round(time.time() - t0, 1)}s", flush=True)


if __name__ == "__main__":
    main()
