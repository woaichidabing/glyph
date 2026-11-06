"""掌握度复测的真模型评测（integration，默认跳过，用 -m integration 运行）。

量化第七棒的三件事：
1. **判题一致率**：强模型对金标 54 条判例（每题 independent/hinted/wrong 三档）判档，
   与金标比对——测它守不守"答案对错 × 是否给过提示 → 三档"的规则；
2. **出题结构合规率**：强模型按每道金标的历史错句种子当场换语境出新题，做确定性
   结构校验（考点一致、题型合法、封闭答案、填空有挖空、题面不漏答案、不出括号即
   答案的废题）；
3. **出题语义正确率**：结构管不了"标准答案本身对不对、改错句是否真有错、有没有跑题"，
   再让同一个强模型扮演**独立审题员**（给足评分规则）逐题审查，三项全对才算 sound。
   实测给足规则后模型能稳定抓出自己出的错题（如把正确的 an honest 错改成 a honest）。

出题/判题/审题按用户要求**走强模型（grade_model）**，不用便宜档。审题只在离线评测跑，
不进真机复测链路（省 token；真机靠出题提示词 + 用户终审兜底）。

运行（在 markr/ 目录）：
    .venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration \\
        tests/tools/test_mastery_review_integration.py -s
阈值 0.90 是质量基线；模型或提示词改动后用它量化回归，数字变化要在 REVIEW 留痕。
"""

from __future__ import annotations

import json

import openai
import pytest
from mastery_review_eval import (
    CASES_PATH,
    evaluate_answer_correctness,
    evaluate_generation,
    evaluate_judging,
    load_cases,
)

from glyph.config import load_settings

_REPORT_PATH = CASES_PATH.parent / "last_run.json"


@pytest.mark.integration
async def test_判题一致率_出题结构合规率_语义正确率():
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过真模型复测评测")

    data = load_cases()
    assert data.get("questions"), "金标集为空，无法评测"

    # 出题/判题/审题用批改同款强模型（grade_model），不用便宜的对话档
    client = openai.AsyncOpenAI(api_key=settings.api_key, base_url=settings.base_url)
    model = settings.grade_model

    judging = await evaluate_judging(client, model, data)
    generation = await evaluate_generation(client, model, data)
    audit = await evaluate_answer_correctness(client, model, data, generation)

    print("\n================ 判题一致率（金标三档）================")
    print(
        f"模型={model}  总判例={judging['total']}  一致={judging['correct']}  "
        f"一致率={judging['accuracy']:.3f}"
    )
    print("按考点一致率:", judging["per_code"])
    print("混淆矩阵 gold→pred:", judging["confusion"])
    for m in judging["mismatches"]:
        print(
            f"  ❗ {m['id']} [{m['code']}] 答案={m['answer']!r} hint={m['hint_given']} "
            f"金标={m['gold']} 模型判={m['pred']}"
        )

    print("\n================ 出题结构合规率 ================")
    print(
        f"总题={generation['total']}  合规={generation['compliant']}  "
        f"合规率={generation['compliance_rate']:.3f}"
    )
    for f in generation["failures"]:
        print(f"  ❗ {f['id']} [{f['code']}]: {'；'.join(f['issues'])}")

    print("\n================ 出题语义正确率（独立审题）================")
    print(
        f"审题={audit['total']}  无误={audit['sound']}  "
        f"语义正确率={audit['sound_rate']:.3f}  技术失败跳过={audit['skipped']}"
    )
    for f in audit["failures"]:
        print(f"  ❗ {f['id']} [{f['code']}]: {f['reason']}")

    _REPORT_PATH.write_text(
        json.dumps(
            {
                "model": model,
                "judging": judging,
                "generation": generation,
                "answer_audit": audit,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    assert judging["accuracy"] >= 0.90, (
        f"判题对金标一致率 {judging['accuracy']:.3f} 低于 0.90，先看混淆矩阵和明细"
    )
    assert generation["compliance_rate"] >= 0.90, (
        f"出题结构合规率 {generation['compliance_rate']:.3f} 低于 0.90"
    )
    assert audit["sound_rate"] >= 0.90, (
        f"出题语义正确率 {audit['sound_rate']:.3f} 低于 0.90，有错题会教错学生"
    )
