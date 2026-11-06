import { describe, it, expect } from "vitest";
import { extractScores, scoreBand, averageScore, DIMENSIONS } from "./scoreCard";

describe("extractScores", () => {
  it("解析四维分数与点评", () => {
    const s = extractScores({
      content: { score: 72, comment_zh: "观点清楚" },
      structure: { score: 68, comment_zh: "衔接偏少" },
      accuracy: { score: 55, comment_zh: "时态错误多" },
      richness: { score: 61, comment_zh: "词汇单一" },
    });
    expect(s).not.toBeNull();
    expect(s!.content.score).toBe(72);
    expect(s!.accuracy.comment_zh).toBe("时态错误多");
    expect(DIMENSIONS.map((d) => d.label)).toEqual(["内容", "结构", "准确", "丰富"]);
  });

  it("分数越界收敛到 0-100", () => {
    const s = extractScores({
      content: { score: 130 },
      structure: { score: -8 },
      accuracy: { score: 88.6 },
      richness: { score: 90 },
    })!;
    expect(s.content.score).toBe(100);
    expect(s.structure.score).toBe(0);
    expect(s.accuracy.score).toBe(89);
  });

  it("某维 null（不适用）保留 null，其余照常", () => {
    const s = extractScores({
      content: { score: 70 },
      structure: { score: null, comment_zh: "单句不评结构" },
      accuracy: { score: 80 },
      richness: { score: 65 },
    })!;
    expect(s.structure.score).toBeNull();
    expect(s.structure.comment_zh).toBe("单句不评结构");
    expect(averageScore(s)).toBe(72); // (70+80+65)/3 ≈ 71.7 → 72
  });

  it("四维全空 / 整块缺失 / 非对象 → null", () => {
    expect(extractScores({ content: { score: null }, structure: {}, accuracy: "x", richness: null })).toBeNull();
    expect(extractScores(null)).toBeNull();
    expect(extractScores(undefined)).toBeNull();
    expect(extractScores("nope")).toBeNull();
  });

  it("非数字分数当作不适用，不影响其它维度", () => {
    const s = extractScores({
      content: { score: "70" }, // 前端只认真数字（后端已收敛过）
      structure: { score: 70 },
      accuracy: { score: 70 },
      richness: { score: 70 },
    })!;
    expect(s.content.score).toBeNull();
    expect(averageScore(s)).toBe(70);
  });
});

describe("scoreBand", () => {
  it("按档位返回配色等级", () => {
    expect(scoreBand(90)).toBe("high");
    expect(scoreBand(75)).toBe("high");
    expect(scoreBand(74)).toBe("mid");
    expect(scoreBand(60)).toBe("mid");
    expect(scoreBand(59)).toBe("low");
    expect(scoreBand(40)).toBe("low");
    expect(scoreBand(39)).toBe("bad");
    expect(scoreBand(0)).toBe("bad");
    expect(scoreBand(null)).toBe("none");
  });
});
