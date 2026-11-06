import { describe, expect, it } from "vitest";
import {
  countsChips,
  countsText,
  groupPapers,
  paperGroupTitle,
  paperTitle,
  type PaperSummary,
} from "./quizLibrary";

/** 造一套卷子的汇总（对齐后端 GET /api/quiz/list 的 paper 载荷） */
const paper = (
  key: string,
  overrides: Partial<PaperSummary> = {},
): PaperSummary => ({
  key,
  exam: "四级",
  year: "2026",
  source: null,
  counts: { 仔细阅读: 2, 段落匹配: 1, 选词填空: 1, 写作: 1 },
  card_count: 5,
  builtin: false,
  updated_at: null,
  ...overrides,
});

describe("paperTitle", () => {
  it("来源（套次名）非空就直接用", () => {
    expect(paperTitle(paper("cet4-2026-06-set1", { source: "第1套" }))).toBe("第1套");
  });

  it("来源为空时从 key 里的 setN 还原「第 N 套」（批量导入的情形）", () => {
    expect(paperTitle(paper("cet4-2026-set2"))).toBe("第2套");
    expect(paperTitle(paper("cet4-2026-set3"))).toBe("第3套");
  });

  it("既没来源也没 setN，就用 key 原文兜底（高考卷那种整卷命名）", () => {
    expect(paperTitle(paper("2024新课标Ⅰ卷", { exam: "高考" }))).toBe("2024新课标Ⅰ卷");
  });
});

describe("paperGroupTitle", () => {
  it("考试 + 四位年份拼成「四级 · 2026年」", () => {
    expect(paperGroupTitle(paper("k", { exam: "四级", year: "2026" }))).toBe("四级 · 2026年");
  });

  it("只有考试就只显示考试", () => {
    expect(paperGroupTitle(paper("k", { exam: "雅思", year: null }))).toBe("雅思");
  });

  it("考试年份都缺，归到「未分组」而不是露出空白或技术 key", () => {
    expect(paperGroupTitle(paper("k", { exam: null, year: null }))).toBe("未分组");
  });
});

describe("countsChips / countsText", () => {
  it("按固定题型顺序报篇数，缺的题型不显示", () => {
    const chips = countsChips(paper("k", { counts: { 写作: 1, 仔细阅读: 2 } }));
    expect(chips.map(c => c.label)).toEqual(["仔细阅读", "写作"]);
    expect(chips.map(c => c.n)).toEqual([2, 1]);
  });

  it("拼成一行人话", () => {
    expect(countsText(paper("k", { counts: { 仔细阅读: 2, 写作: 1 } }))).toBe(
      "仔细阅读 2 · 写作 1",
    );
  });

  it("一张卡都没有时给空串，不渲染出空的中点", () => {
    expect(countsText(paper("k", { counts: {} }))).toBe("");
  });
});

describe("groupPapers", () => {
  it("按考试年份分组，组内沿用后端的最近导入顺序", () => {
    const groups = groupPapers([
      paper("cet4-2026-set2", { updated_at: "2026-10-10T10:00:00" }),
      paper("cet4-2026-set1", { source: "第1套", updated_at: "2026-10-09T10:00:00" }),
      paper("cet6-2026-set1", { exam: "六级", updated_at: "2026-10-08T10:00:00" }),
    ]);
    expect(groups.map(g => g.title)).toEqual(["四级 · 2026年", "六级 · 2026年"]);
    expect(groups[0].papers.map(p => p.key)).toEqual(["cet4-2026-set2", "cet4-2026-set1"]);
    expect(groups[1].papers.map(p => p.key)).toEqual(["cet6-2026-set1"]);
  });

  it("空列表返回空分组", () => {
    expect(groupPapers([])).toEqual([]);
  });
});
