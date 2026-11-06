"""#21 真机探针：上下文自动压缩在真实 API 上的端到端验收（可复跑）。

与 tests/runtime/test_compaction_e2e.py（离线、脚本化模型）相对：这里用 markr/.env 的
聚合 key，**真实调用 glm-5.2（主脑，chat 通道）与 qwen3.7-flash（L3 浓缩写手）**，灌一条
约百万字节、自然英文的长学习会话，验证：

  1. 首轮只做 L1；服务端真实回传 input_tokens（A2 记账）；
  2. 第二轮按真实水位触发 L3：started→（写手真实摘要）→done，after<before；
  3. 账本真落 COMPACT_SUMMARY / COMPACT_BOUNDARY，切片后最旧原文移出 body、摘要在；
  4. D2 指标真落一行 JSON；
  5. read_artifact 仍能从全量流水取回最早那篇批改成品（压不得清单）。

运行（在 markr/ 目录）：
    set PYTHONPATH=src
    .venv\\Scripts\\python.exe scripts\\probe_compaction_real.py
可用环境变量 TARGET_BYTES 调灌入规模（默认 1_000_000 自然英文字节）。
真机花费：glm-5.2 两次约 25 万 token 输入 + flash 一次约 15 万材料，合计约 ¥1 内。

2026-10-04 实测结论（标准档 W=300k，L2 线 150k / L3 线 210k / 硬线 288k）：
- 1015KB 自然文本（glm-5.2，run1 真实 input=208,642）：只过 L2 线 → run2 触发 L2 装箱，
  主脑在提货单提示下**真实调用 read_artifact 取回最早批改**并正确答出 g1（go→went），
  L2 透明文案正常出现，全程 final_text。L2 + 按需取回闭环真机通过。
- 1201KB（glm-5.2，run1 真实 input=246,667）：run2 完整走 L3——started(l2+l3,247,301)
  → qwen3.7-flash 写手 38.7s 产出高质量摘要（保住 grade 代号/错误点/进行中任务，还如实
  标注语料重复）→ done(247,301→178,320，material_truncated=false)，账本落 SUMMARY+
  BOUNDARY、D2 jsonl 落一行 success。压缩后主脑重发那一步返回 **403 Free quota
  exhausted**：经最小 "hi" 请求复验，glm-5.2 在本把 FreeTierOnly key 上免费额度已彻底
  耗尽（与长度无关），写手 qwen3.7-flash / qwen3.7-max / qwen3.8-max 均正常。压缩本身
  已成功落账，loop 按 §12.4 把 403 兜底成 ErrorEvent、未炸、账本完好。
  → 发布提示：默认主脑若定 glm-5.2，需用户充值或关闭"仅免费额度"，否则长会话连续调用
    会 403；额度受限时 qwen3.8-max 闭环完整、更稳。
- 1201KB（GLYPH_QWEN_MODEL=qwen3.8-max，表内主脑、标准档 300k）：**L3 后主脑切片续答
  完整闭环，全程 final_text**。L3 在 run1 内部工具迭代间触发（A2 记账在"单次 run 多轮
  工具迭代"与"跨 run"两个尺度都生效）：before=250,869 → after=178,161，写手 25.4s、
  不截断；压缩后主脑在切片视图上继续多轮工作，run2 凭提货单 + read_artifact 详答 g1。
- 副作用观察：把主脑设成**不在 MAIN_MODELS 表**的型号（如 qwen3.7-max）时，有效窗口
  静默回退 DEFAULT_CONTEXT_WINDOW=128k，reserve 占比 70%、tail 预算被挤空（emergency
  after 仅 423 token），判为主动 blocked 应急。该极端路径靠提货单 + read_artifact 仍自救
  成功（§10 真机验证），但表外型号应在装配层给警告（后续健壮性项，非本次目标）。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import openai

from glyph.config import load_settings
from glyph.llm.compactor import CompactionSummarizer
from glyph.llm.qwen import QwenLLM
from glyph.runtime.compaction_metrics import file_compaction_observer
from glyph.runtime.loop import AgentLoop
from glyph.runtime.registry import ToolRegistry
from glyph.session import new_session
from glyph.session.records import RecordType, SummaryPayload
from glyph.tools.factory import build_read_artifact_tool
from glyph.types import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResult,
)

TARGET_BYTES = int(os.environ.get("TARGET_BYTES", "1_000_000"))

# 自然英文学习语料池（作文 / 批改 / 讲解 / 邮件 / 议论 / 词汇），整句重复仍按正常 BPE 计费，
# 用来把会话顶到真实 20 万+ token；每轮带序号标记，便于断言"最旧一篇被移出 body"。
ESSAY_POOL = [
    "Last weekend I visited my grandmother in the countryside. The weather was warm and "
    "the sky was a clear, bright blue. She cooked rice and fish for lunch, and we sat in "
    "her small garden talking about school. She told me stories about when she was young, "
    "how she walked five kilometres each day to reach the nearest classroom. I listened "
    "carefully and realised how lucky I am to have a bus and a library close to my home.",
    "Some people believe that homework should be abolished in primary school. I disagree. "
    "Homework builds discipline and helps children review what they learned the same day, "
    "when the lesson is still fresh. However, the amount must be reasonable; two hours of "
    "exercises every night only exhausts young learners and leaves no time for play or sleep. "
    "A balanced thirty minutes, focused on the hardest ideas, would serve students far better.",
    "Dear Sir or Madam, I am writing to inquire about the part-time English course advertised "
    "on your website. I work full time from Monday to Friday, so I would prefer an evening or "
    "weekend class. Could you please tell me whether such a schedule is available, how much "
    "the tuition costs, and whether the materials are included? I look forward to your reply. "
    "Yours faithfully, Li Ming.",
    "Your paragraph uses the present simple correctly for habits, but there are three small "
    "problems. First, 'He don't like tea' should be 'He doesn't like tea', because the third "
    "person singular needs doesn't. Second, 'I go to school yesterday' mixes tenses; since "
    "yesterday is finished time, use 'I went'. Third, add 'the' before 'library' when you mean "
    "the one both of us know. Keep practising and these will soon feel automatic.",
    "The words 'affect' and 'effect' are often confused. 'Affect' is almost always a verb, as "
    "in 'the weather affects my mood'. 'Effect' is usually a noun meaning a result, as in 'the "
    "medicine had a quick effect'. A useful memory trick: affect starts with an a for action, "
    "while effect ends with a t for the result. Once you remember that action words are verbs, "
    "choosing between the two becomes much easier in your own writing.",
    "Technology has changed the way we learn languages. Students no longer rely only on thick "
    "dictionaries and cassette tapes; they can watch subtitled videos, chat with language "
    "partners abroad, and receive instant feedback from apps. Still, no application can replace "
    "deliberate practice. A learner who repeats sentences aloud and writes a short diary every "
    "day will progress faster than one who merely scrolls through lessons without speaking.",
]

FEEDBACK_POOL = [
    "Nice work on this piece. Your ideas are clear and your opening sentence catches the "
    "reader's attention. Watch your verb tenses when you describe finished events in the past. "
    "Try to vary your sentence openings instead of beginning every sentence with 'I'. Read the "
    "text aloud once before you submit it; you will often hear the missing articles yourself.",
    "This is a strong argument. You state your position early and give two concrete reasons. To "
    "improve, add one short example for each reason, because examples make abstract claims "
    "persuasive. Your conclusion repeats the introduction too closely; instead, restate your view "
    "in fresh words and end with a small recommendation. Vocabulary range is already quite good.",
    "Your email is polite and well organised, and you included all the necessary questions. In "
    "formal writing, avoid contractions such as 'don't' and 'can't'; write 'do not' and 'cannot'. "
    "Use 'I would be grateful if you could...' instead of 'Please tell me...' to sound more "
    "natural. The closing 'Yours faithfully' is correct when you open with 'Dear Sir or Madam'.",
]


def _fill(pool: list[str], target: int, tag: str) -> str:
    out = tag + "\n"
    i = 0
    while len(out.encode("utf-8")) < target:
        out += pool[i % len(pool)] + "\n"
        i += 1
    return out


async def main() -> None:
    settings = load_settings()
    if not settings.api_key:
        raise SystemExit("未在 .env 找到 API key（GLYPH_APIKEY / DASHSCOPE_API_KEY）。")
    print(
        f"[cfg] main_model={settings.model} mode={settings.context_mode} "
        f"auto_compact={settings.auto_compact} compact_model={settings.compact_model}"
    )

    work = Path.home() / ".markr" / "probe_real"
    work.mkdir(parents=True, exist_ok=True)
    metrics_path = work / "compaction.jsonl"
    if metrics_path.exists():
        metrics_path.unlink()

    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=300.0)
    llm = QwenLLM(settings, client=client)
    compactor = CompactionSummarizer(settings, client=client)

    history = await new_session(
        system_prompt="你是一位耐心的英语学习教练，根据本会话里学习者的练习记录作答。",
        model=settings.model,
        cwd=str(work),
        root=work,
    )

    # 最早一篇真实批改成品（read_artifact 的取回对象，带独特标记）。
    grade_call = "grade_real_1"
    essay0 = "REAL-GRADE-SOURCE Yesterday I go to park and see three dog. " + "e" * 2000
    await history.record_user_message("帮我批改这篇：" + essay0)
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id=grade_call, name="grade_text", arguments={"text": essay0})]
        )
    )
    await history.record_tool_result(
        grade_call,
        ToolResult(
            tool_name="grade_text",
            ok=True,
            output={
                "summary": "REAL-GRADE-SUMMARY 三处时态/单复数问题。",
                "detections": [
                    {"id": "g1", "message": "REAL-GRADE-DETAIL go 应改为 went（一般过去时）。"}
                ],
            },
        ),
    )
    await history.record_assistant_message(
        AssistantMessage(blocks=[TextBlock(text="批改好了，最早这篇有三处小问题。")])
    )

    # 灌入自然英文长往返，直到总字节达标（让最早的批改关闭、装箱，并把水位顶过 L3 线）。
    total = 0
    turn = 0
    while total < TARGET_BYTES:
        u = _fill(ESSAY_POOL, 11_000, f"[essay-marker-{turn}]")
        a = _fill(FEEDBACK_POOL, 9_000, f"[feedback-marker-{turn}]")
        await history.record_user_message(u)
        await history.record_assistant_message(AssistantMessage(blocks=[TextBlock(text=a)]))
        total += len(u.encode()) + len(a.encode())
        turn += 1
    print(f"[seed] 灌入 {turn} 个长往返，约 {total / 1000:.0f} KB 自然文本")

    registry = ToolRegistry()
    registry.register(build_read_artifact_tool(history_provider=lambda: history))
    loop = AgentLoop(
        llm,
        registry,
        history=history,
        context_mode=settings.context_mode,
        compactor=compactor,
        compaction_observer=file_compaction_observer(metrics_path),
    )

    async def drain(agen):
        evs = []
        async for e in agen:
            evs.append(e)
        return evs

    # ---- run1：首轮只做 L1，拿到服务端真实 input_tokens ----
    r1 = await drain(loop.run("用一句话说明：按这些练习记录，我现在大概学到什么程度了？"))
    res1 = r1[-1]
    print(
        f"[run1] finish={getattr(res1, 'finish_reason', None)} "
        f"真实 input_tokens={getattr(res1, 'context_input_tokens', None)} "
        f"usage={getattr(res1, 'usage', None)}"
    )
    print(f"[run1] 回答节选：{(res1.text or '')[:120]}")

    # ---- run2：应按真实水位触发 L3 ----
    r2 = await drain(loop.run("我们最早练的那篇作文，你当时指出的第一个问题是什么？请详细说。"))
    res2 = r2[-1]
    comp_events = [e for e in r2 if e.type == "compaction"]
    print("\n[run2] 压缩事件序列：")
    for e in comp_events:
        print("   ", json.dumps(_event_dict(e), ensure_ascii=False))
    print(f"[run2] finish={getattr(res2, 'finish_reason', None)}")
    print(f"[run2] 回答节选：{(res2.text or '')[:300]}")

    types_ = [r.record_type for r in history._records]
    n_sum = types_.count(RecordType.COMPACT_SUMMARY)
    n_bnd = types_.count(RecordType.COMPACT_BOUNDARY)
    print(f"\n[ledger] COMPACT_SUMMARY={n_sum} COMPACT_BOUNDARY={n_bnd}")
    if n_sum:
        rec = next(r for r in history._records if r.record_type == RecordType.COMPACT_SUMMARY)
        payload = rec.payload_as(SummaryPayload)
        print(f"[ledger] 真实摘要前 400 字：\n{payload.text[:400]}")

    print(f"\n[metric] 文件={metrics_path}")
    if metrics_path.exists():
        for line in metrics_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                print("   ", line)

    recovered = history.read_artifact(grade_call)
    if recovered is not None:
        print(f"\n[read_artifact] 取回成功：title={recovered.get('title')!r}")
        print(f"[read_artifact] detections={str(recovered.get('detections'))[:200]}")
        print(
            f"[read_artifact] source 含 REAL-GRADE-SOURCE："
            f"{'REAL-GRADE-SOURCE' in recovered.get('source_text', '')}"
        )
    else:
        print("\n[read_artifact] 取回失败：返回 None")

    history.close()
    print("\n[done] 真机探针结束。")


def _event_dict(e) -> dict:
    keys = (
        "phase",
        "level",
        "message",
        "before_tokens",
        "after_tokens",
        "boundary_id",
        "material_truncated",
        "elapsed_ms",
    )
    return {k: getattr(e, k, None) for k in keys if getattr(e, k, None) is not None}


if __name__ == "__main__":
    asyncio.run(main())
