import { describe, it, expect } from "vitest";
import { extractErrorsFromFinalize } from "./errorExtract";
import type { DetectionDTO } from "./batch";

const D = (over: Partial<DetectionDTO>): DetectionDTO => ({
  id: over.id ?? "d",
  original: over.original ?? "",
  corrected: over.corrected ?? "",
  tier: over.tier ?? "error",
  type: over.type ?? "hard",
  explanation_zh: over.explanation_zh ?? "",
  start: over.start ?? null,
  end: over.end ?? null,
});

const ESSAY = "Yesterday I goes to school. It were fun. I think that the government should take measure to solve the problem which is very seriously.";

describe("extractErrorsFromFinalize", () => {
  it("硬错、词级修改收录：正确形式为词条，带错误形式/原因/出处日期", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d1", original: "goes", corrected: "went", explanation_zh: "yesterday 是过去时间，用过去式 went。", start: 11, end: 15 })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      word: "went",
      wrong: "goes",
      from: "2026-10-09 作文批改",
    });
    expect(out[0].sentence).toContain("goes to school");
    expect(out[0].reason).toContain("went");
  });

  it("润色建议（tier=suggestion / type=soft）不收——错词本只收真正写错的", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "s1", original: "good", corrected: "excellent", tier: "suggestion", type: "soft", explanation_zh: "可以用更高级的词" })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out).toEqual([]);
  });

  it("整句改写（正确形式超过 2 个词）不收——那是句法/表达，不是错词", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d2", original: "solve the problem which is very seriously",
        corrected: "address the very serious problem", tier: "error", explanation_zh: "语序问题" })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out).toEqual([]);
  });

  it("两词以上的短语不收（错词本按词查词典，短语只会变成哑条目）", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d3", original: "take measure", corrected: "take measures",
        explanation_zh: "measure 作措施讲常用复数", start: 60, end: 72 })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out).toEqual([]);
  });

  it("单个词照收（went / measures）", () => {
    const out = extractErrorsFromFinalize({
      kept: [
        D({ id: "d4", original: "go", corrected: "went", explanation_zh: "过去式", start: 0, end: 2 }),
        D({ id: "d5", original: "measure", corrected: "measures", explanation_zh: "复数", start: 60, end: 67 }),
      ],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out.map((e) => e.word)).toEqual(["went", "measures"]);
  });

  it("同一正确形式在篇内重复只收一次", () => {
    const out = extractErrorsFromFinalize({
      kept: [
        D({ id: "a", original: "were", corrected: "was", explanation_zh: "主谓一致" }),
        D({ id: "b", original: "were", corrected: "was", explanation_zh: "主谓一致" }),
      ],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out).toHaveLength(1);
  });

  it("句首大写的正确形式归一为小写词条", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d4", original: "go", corrected: "Went", explanation_zh: "过去式" })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out[0].word).toBe("went");
  });

  it("缺 corrected / 缺 original 的脏数据不收", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d5", corrected: "", original: "xxx" }), D({ id: "d6", corrected: "was", original: "" })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out).toEqual([]);
  });

  it("OCR 文本带标题行时，句子语境不把标题切进来（换行也是句子边界）", () => {
    // "My Weekend\n" 占 11 个字符（0..10），正文第一句从 11 开始；go 位于 26..28
    const text = "My Weekend\nLast weekend I go to the park.";
    expect(text.slice(26, 28)).toBe("go");
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d8", original: "go", corrected: "went", explanation_zh: "过去式", start: 26, end: 28 })],
      sourceText: text,
      today: "2026-10-09",
    });
    expect(out[0].sentence).toBe("Last weekend I go to the park.");
  });

  it("句子语境不跨段落（换行即截断，不带入下一段）", () => {
    const text = "It were fun.\nNext paragraph starts here.";
    // were 位于 3..7
    expect(text.slice(3, 7)).toBe("were");
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d9", original: "were", corrected: "was", explanation_zh: "主谓一致", start: 3, end: 7 })],
      sourceText: text,
      today: "2026-10-09",
    });
    expect(out[0].sentence).toBe("It were fun.");
  });

  it("没有坐标时 sentence 兜底为空串，reason 给人话兜底", () => {
    const out = extractErrorsFromFinalize({
      kept: [D({ id: "d7", original: "were", corrected: "was", explanation_zh: "" })],
      sourceText: ESSAY,
      today: "2026-10-09",
    });
    expect(out[0].sentence).toBe("");
    expect(out[0].reason).toBe("作文里写成了 were，正确是 was");
  });
});
