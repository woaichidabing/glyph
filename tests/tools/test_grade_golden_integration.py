"""金标集成测试（真模型，默认跳过，用 -m integration 运行）。

端到端跑 grade_text（真 pro 批改器 + 真本地拼写器），对 39 条金标算
召回 / 精确 / F0.5，并守两条"别乱改"红线：正确句零误报、可接受句不报 error。

运行（在 markr/ 目录）：
    .venv\\Scripts\\python.exe -m pytest -o addopts="" -m integration \\
        tests/tools/test_grade_golden_integration.py
阈值是质量基线，不是凑数；模型迭代后用本测试量化回归，数字变化要在 REVIEW 留痕。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from golden_eval import aggregate, score_case

from glyph.config import load_settings
from glyph.runtime.registry import ToolRegistry
from glyph.tools import build_grade_text_tool

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "golden" / "cases.json"


def _load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


@pytest.mark.integration
async def test_grade_text_on_golden_set():
    settings = load_settings()
    if not settings.api_key:
        pytest.skip("未配置 API key，跳过真模型金标测试")

    cases = _load_cases()
    registry = ToolRegistry()
    registry.register(build_grade_text_tool(settings))

    sem = asyncio.Semaphore(4)

    async def run_one(case: dict) -> dict:
        async with sem:
            result = await registry.execute("grade_text", {"text": case["text"]})
        if not result.ok:
            # 工具整体失败：把该 case 记成全漏 + 一条误报占位，便于在明细里看到
            pytest.fail(f"{case['id']} grade_text 执行失败: {result.error}")
        return score_case(case, result.output.get("detections", []))

    scores = await asyncio.gather(*(run_one(c) for c in cases))
    agg = aggregate(scores)

    # 逐句明细（-s 查看）
    print("\n================ 金标逐句明细 ================")
    for case, s in zip(cases, scores, strict=True):
        flag = {"hard": "  ", "chinglish": "  ", "wordy": "  ", "soft": "  ", "ok": "  "}
        if s["bucket"] in ("hard", "chinglish", "wordy"):
            flag[s["bucket"]] = (
                "✅" if s["fn"] == 0 and s["fp"] == 0 else ("⚠️漏" if s["fn"] else "❌多")
            )
        elif s["bucket"] == "ok":
            flag["ok"] = "  " if s["fp"] == 0 else "❌误报"
        else:
            flag["soft"] = "  " if s["fp"] == 0 else "❌过度"
        head = (
            f"{flag[s['bucket']]} {s['id']} [{s['bucket']:^9}] "
            f"TP={s['tp']} FP={s['fp']} FN={s['fn']}  {case['text'][:60]}"
        )
        print(head)
        for g in s["unmatched_golds"]:
            print(f"      漏报 gold: {g}")
        for d in s["false_dets"]:
            line = (
                f"      误报/过度: {d.get('tier')}/{d.get('type')} "
                f"{d.get('original')!r} -> {d.get('corrected')!r}"
            )
            print(line)
        for d in s.get("extra_suggestions", []):
            print(f"      软建议(不计FP): {d.get('original')!r} -> {d.get('corrected')!r}")

    print("\n================ 汇总 ================")
    print(f"TP={agg['tp']} FP={agg['fp']} FN={agg['fn']}")
    print(f"Precision={agg['precision']:.3f}  Recall={agg['recall']:.3f}  F0.5={agg['f0.5']:.3f}")
    print("分桶召回:", {k: round(v, 3) for k, v in agg["bucket_recall"].items()})
    print("OK 误报句:", agg["ok_false_positive_cases"])
    print("SOFT 过度句:", agg["soft_over_error_cases"])
    print("额外软建议数(不计FP):", agg["extra_suggestions"])

    # 落盘完整明细，便于复查与质量回归留痕（assert 失败也已写盘）
    report = {
        "model": settings.grade_model,
        "summary": {
            "tp": agg["tp"],
            "fp": agg["fp"],
            "fn": agg["fn"],
            "precision": agg["precision"],
            "recall": agg["recall"],
            "f0.5": agg["f0.5"],
            "bucket_recall": agg["bucket_recall"],
            "ok_false_positive_cases": agg["ok_false_positive_cases"],
            "soft_over_error_cases": agg["soft_over_error_cases"],
            "extra_suggestions": agg["extra_suggestions"],
        },
        "cases": [
            {
                "id": s["id"],
                "bucket": s["bucket"],
                "tp": s["tp"],
                "fp": s["fp"],
                "fn": s["fn"],
                "unmatched_golds": s["unmatched_golds"],
                "false_dets": [
                    {
                        k: d.get(k)
                        for k in (
                            "original",
                            "corrected",
                            "type",
                            "tier",
                            "source",
                            "explanation_zh",
                        )
                    }
                    for d in s["false_dets"]
                ],
                "extra_suggestions": [
                    {
                        k: d.get(k)
                        for k in (
                            "original",
                            "corrected",
                            "type",
                            "tier",
                            "source",
                            "explanation_zh",
                        )
                    }
                    for d in s.get("extra_suggestions", [])
                ],
            }
            for s in scores
        ],
    }
    (CASES_PATH.parent / "last_run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 红线：正确句不许乱改、可接受句不许报 error
    assert agg["ok_false_positive_cases"] == [], "正确句出现误报（过度批改）"
    assert agg["soft_over_error_cases"] == [], "可接受句被报成 error（过度批改）"
    # 质量基线（先跑出新口径真实数字后再收紧；主要靠明细人工复查）
    assert agg["recall"] >= 0.85, f"召回 {agg['recall']:.3f} 低于 0.85"
    assert agg["f0.5"] >= 0.85, f"F0.5 {agg['f0.5']:.3f} 低于 0.85"
    assert agg["extra_suggestions"] <= 12, "额外软建议过多，疑似建议泛滥"
