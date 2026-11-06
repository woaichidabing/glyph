import { describe, expect, it } from "vitest";
import { buildFinalizePayload, renderFinalText, type DetectionDecisionMap, type MarkSeg } from "./finalize";
import type { DetectionDTO } from "./batch";

const det = (d: Partial<DetectionDTO>): DetectionDTO => ({
  id: "d1", original: "go", corrected: "went", type: "hard", tier: "error",
  explanation_zh: "过去式", source: "model", ...d,
});

describe("buildFinalizePayload（拍板 → 定稿请求体）", () => {
  const text = "I go to school yesterday.";

  it("全采纳：每条 decisions=accepted，无 overrides / edited_ids", () => {
    const p = buildFinalizePayload({
      text,
      detections: [det({ id: "d1" }), det({ id: "d2", corrected: "goes" })],
      decisions: { d1: { action: "accepted" }, d2: { action: "accepted" } },
    });
    expect(p.text).toBe(text);
    expect(p.decisions).toEqual({ d1: "accepted", d2: "accepted" });
    expect(p.overrides).toEqual({});
    expect(p.edited_ids).toEqual([]);
    expect(p.comment).toBe("");
    // detections 原样回传（后端要重新校验的完整对象）
    expect(p.detections).toHaveLength(2);
    expect(p.detections[1].corrected).toBe("goes");
  });

  it("混合拍板：edited 进 overrides 和 edited_ids；rejected 与未拍板都是 rejected", () => {
    const decisions: DetectionDecisionMap = {
      d1: { action: "accepted" },
      d2: { action: "edited", override: "goed" },
      // d3 未拍板
      d4: { action: "rejected" },
    };
    const p = buildFinalizePayload({
      text,
      detections: [
        det({ id: "d1" }), det({ id: "d2" }), det({ id: "d3" }), det({ id: "d4" }),
      ],
      decisions,
      comment: "谢谢",
    });
    expect(p.decisions).toEqual({
      d1: "accepted", d2: "edited", d3: "rejected", d4: "rejected",
    });
    expect(p.overrides).toEqual({ d2: "goed" });
    expect(p.edited_ids).toEqual(["d2"]);
    expect(p.comment).toBe("谢谢");
  });

  it("edited 但 override 为空：降级为 accepted，不进 overrides", () => {
    const p = buildFinalizePayload({
      text,
      detections: [det({ id: "d1" })],
      decisions: { d1: { action: "edited", override: "   " } },
    });
    expect(p.decisions.d1).toBe("accepted");
    expect(p.overrides).toEqual({});
    expect(p.edited_ids).toEqual([]);
  });

  it("个别 detection 没有 id：不崩、不进 decisions（后端无法对它表态）", () => {
    const p = buildFinalizePayload({
      text,
      detections: [det({ id: null }), det({ id: "d2" })],
      decisions: { d2: { action: "accepted" } },
    });
    expect(p.decisions).toEqual({ d2: "accepted" });
    expect(p.detections).toHaveLength(2);
  });

  it("拍板进度统计：decidedCount / keptCount / allDecided", () => {
    const stats = buildFinalizePayload({
      text,
      detections: [det({ id: "d1" }), det({ id: "d2" }), det({ id: "d3" })],
      decisions: {
        d1: { action: "accepted" },
        d2: { action: "edited", override: "x" },
      },
    });
    expect(stats.progress).toEqual({ decided: 2, total: 3, kept: 2, allDecided: false });
  });
});

describe("renderFinalText（定稿全文 + 绿色高亮改后片段）", () => {
  it("没有 marks：整段转义输出，不产生高亮", () => {
    expect(renderFinalText("a < b & c > 0", [])).toBe("a &lt; b &amp; c &gt; 0");
  });

  it("按 added 段在全文中顺序定位并包 <mark>，换行转 <br>", () => {
    const finalText = "I went home.\nIt was fine.";
    const marks: MarkSeg[] = [
      { kind: "plain", text: "I " },
      { kind: "removed", text: "go" },
      { kind: "added", text: "went", index: 1 },
      { kind: "plain", text: " home.\nIt was fine." },
    ];
    expect(renderFinalText(finalText, marks)).toBe(
      'I <mark class="dd-hl">went</mark> home.<br>It was fine.',
    );
  });

  it("同一个改法在文中出现两次：高亮游标之后的那一处（与批改顺序一致）", () => {
    // 定稿全文：第二处 go 已替换为 went，第一处保持原样
    const finalText = "I go. I went to school.";
    const marks: MarkSeg[] = [
      { kind: "plain", text: "I go. I " },
      { kind: "removed", text: "go" },
      { kind: "added", text: "went", index: 1 },
      { kind: "plain", text: " to school." },
    ];
    expect(renderFinalText(finalText, marks)).toBe("I go. I <mark class=\"dd-hl\">went</mark> to school.");
  });

  it("added 文本在定稿里找不到（数据对不上）：跳过不高亮，也不崩", () => {
    const html = renderFinalText("Nothing matches here.", [
      { kind: "added", text: "went", index: 1 },
    ]);
    expect(html).toBe("Nothing matches here.");
    expect(html).not.toContain("<mark");
  });
});
