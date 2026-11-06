"""整条链路手工走查：图片 → OCR → 批改 → 逐条裁决 → 复核 → 定稿。

**不是自动化测试**（接缝还在变，固化成测试会变成维护负担），只用于人工走一遍并留实测记录。

跑法（需 markr/.env 配好 key，样张用环境变量给）：

    .venv\\Scripts\\python examples\\e2e_chain_manual.py

    # 指定样张（不指定就用内置短文）
    set GLYPH_OCR_SAMPLE_IMAGE=..\\legacy\\.tmp_eval\\images\\xx.jpg
    .venv\\Scripts\\python examples\\e2e_chain_manual.py

没给样张就跳过 OCR 段，改用内置的一段手写风英文短文继续往下走。

每一步打印耗时，用来回答"整条链到底慢在哪"。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from glyph.config import load_settings
from glyph.tools import (
    build_grade_text_tool,
    build_ocr_image_tool,
    build_recheck_detection_tool,
)
from glyph.tools.grading_models import Detection
from glyph.tools.review import (
    Decision,
    apply_decisions,
    build_marked_segments,
    build_result_text,
    finalize_text,
)

FALLBACK_TEXT = (
    "I am writting this essay to talk about my best friend. "
    "She is very kind and she always help me when I have problem. "
    "We know each other since we are children. "
    "In my opinion, a good friend should be honest and trustworthy."
)

# recheck_detection 的入参 schema 是 additionalProperties: False，
# 回带时只挑它认得的字段，避免把 recheck / aligned 之类带进去。
_DETECTION_KEYS = (
    "id",
    "original",
    "corrected",
    "type",
    "tier",
    "source",
    "start",
    "end",
    "aligned",
    "explanation_zh",
)


class _FileProvider:
    """走查专用：直接按文件路径取字节（真实实现由装配层给出）。"""

    def __init__(self, path: Path, mime: str) -> None:
        self._path = path
        self._mime = mime

    async def read(self, image_id: str) -> tuple[bytes, str]:
        return self._path.read_bytes(), self._mime  # noqa: ASYNC240


def _step(title: str, t0: float) -> None:
    print(f"\n[{time.perf_counter() - t0:6.2f}s] ==== {title} ====")


async def main() -> None:
    settings = load_settings()
    if not settings.api_key:
        print("没配 API key，跑不了真模型。请在 markr/.env 里配好。")
        return

    t_start = time.perf_counter()

    # ---- 1. OCR：图片 → 英文文本 ----
    _step("1/5 OCR 转写", t_start)
    text = FALLBACK_TEXT
    raw = os.environ.get("GLYPH_OCR_SAMPLE_IMAGE")
    if raw and Path(raw).is_file():  # noqa: ASYNC240
        path = Path(raw)
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(
            path.suffix.lower(), "image/jpeg"
        )
        ocr = build_ocr_image_tool(_FileProvider(path, mime), settings)
        out = await ocr.run({"image_id": "manual"})
        text = out["text"]
        print(f"样张：{path}")
        print(f"low_confidence={out['low_confidence']}  warnings={out['warnings']}")
        print(f"转写结果（{len(text)} 字符）：\n{text}")
    else:
        print(f"未给样张，用内置短文（{len(text)} 字符）：\n{text}")

    # ---- 2. 批改：文本 → 逐条问题 ----
    t1 = time.perf_counter()
    _step("2/5 grade_text 批改", t_start)
    grade = build_grade_text_tool(settings)
    res = await grade.run({"text": text})
    raw_dets = res["detections"]
    print(f"总评：{res['summary']}")
    print(f"stats={res['stats']}")
    print(f"warnings={res['warnings']}")
    print(f"共 {len(raw_dets)} 条：")
    for i, d in enumerate(raw_dets, 1):
        print(f"  {i}. [{d['type']}/{d['source']}] {d['original']!r} -> {d['corrected']!r}")
        print(f"     {d['explanation_zh']}")
    print(f"批改耗时 {time.perf_counter() - t1:.2f}s")

    if not raw_dets:
        print("\n没批出任何条目，后面的裁决 / 复核 / 定稿没有可走的东西，到此为止。")
        return

    dets = [Detection.model_validate(d) for d in raw_dets]

    # ---- 3. 裁决：老师表态（这里模拟：全采纳，驳回第一条）----
    _step("3/5 逐条裁决", t_start)
    decisions: dict[str, Decision] = {d.id: "accepted" for d in dets if d.id}
    rejected = next((d for d in dets if d.id), dets[0])
    decisions[rejected.id] = "rejected"
    print(f"全部采纳，唯独驳回第 1 条：{rejected.original!r} -> {rejected.corrected!r}")

    # ---- 4. 复核：把驳回那条拿回去重判 ----
    t2 = time.perf_counter()
    _step("4/5 recheck 复核被驳回的那条", t_start)
    payload = {k: getattr(rejected, k) for k in _DETECTION_KEYS}
    recheck_tool = build_recheck_detection_tool(settings)
    rr = await recheck_tool.run(
        {
            "text": text,
            "detection": payload,
            "round": 1,
            "note": "老师认为这里没错",
        }
    )
    print(f"复核状态：{rr['status']}（round={rr['round']}, scope={rr['scope']}）")
    print(f"复核说法：{rr['reason_zh']}")
    print(f"下一步：{rr['follow_up']}")
    print(f"warnings={rr['warnings']}")
    print(f"复核耗时 {time.perf_counter() - t2:.2f}s")

    # ---- 5. 定稿：三个出口 ----
    _step("5/5 定稿三出口", t_start)
    # 复核结论挂回原条目（run 已返回完整的 detection），供 C-2 防御判定
    dets = [Detection.model_validate(rr["detection"]) if d.id == rejected.id else d for d in dets]
    warns: list[str] = []
    kept = apply_decisions(dets, decisions, warnings=warns)  # type: ignore[arg-type]
    print(f"保留 {len(kept)} 条（共 {len(dets)} 条）  warnings={warns}")

    final_text, fin_warns = finalize_text(text, kept)
    marks, mark_warns = build_marked_segments(text, kept)
    result_text = build_result_text(text, kept, comment="整体不错，注意句子之间要断开。")
    print(f"\n--- 出口① 改后全文 ---\n{final_text}")
    print(f"\n--- 出口② 批阅稿 marks（{len(marks)} 段）---")
    for m in marks:
        print(f"  {m.kind:8} idx={m.index} {m.text!r}")
    print(f"\n--- 出口③ 讲解清单 ---\n{result_text}")
    print(f"\nwarnings: finalize={fin_warns} marks={mark_warns}")
    print(f"\n整链总耗时 {time.perf_counter() - t_start:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
