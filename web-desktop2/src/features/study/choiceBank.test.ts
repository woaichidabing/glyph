import { describe, it, expect } from "vitest";
import { buildQuiz, type QuizPoolEntry } from "./choiceBank";

/**
 * 真词书场景：目标词和干扰项都来自后端词书页（带轻量释义），
 * 不再依赖只有几十个词的内置 mini 库。
 */
const POOL: QuizPoolEntry[] = [
  { word: "state", phonetic: "steit", pos: "n.", gloss: "州, 状态" },
  { word: "status", phonetic: "", pos: "n.", gloss: "地位, 情形" },
  { word: "station", phonetic: "", pos: "n.", gloss: "车站, 站" },
  { word: "statement", phonetic: "", pos: "n.", gloss: "陈述, 声明" },
  { word: "system", phonetic: "", pos: "n.", gloss: "系统, 体系" },
  { word: "government", phonetic: "", pos: "n.", gloss: "政府, 内阁" },
  { word: "program", phonetic: "", pos: "n.", gloss: "节目, 程序" },
  { word: "explain", phonetic: "", pos: "vt.", gloss: "说明, 解释" },
];

describe("buildQuiz：用词书页释义出选择题", () => {
  it("目标词不在 mini 库、只在词书池里时，照样能出四选项", () => {
    const quiz = buildQuiz("state", POOL);
    expect(quiz).not.toBeNull();
    expect(quiz!.choices).toHaveLength(4);
    const right = quiz!.choices.filter((c) => c.right);
    expect(right).toHaveLength(1);
    expect(right[0].word).toBe("state");
    expect(right[0].text).toBe("州, 状态");
  });

  it("三个干扰项全部来自词书池，且不与正确释义撞车", () => {
    for (let i = 0; i < 20; i += 1) {
      const quiz = buildQuiz("state", POOL);
      const wrong = quiz!.choices.filter((c) => !c.right);
      expect(wrong).toHaveLength(3);
      expect(wrong.every((c) => c.text !== "州, 状态")).toBe(true);
      expect(wrong.every((c) => POOL.some((p) => p.word === c.word))).toBe(true);
    }
  });

  it("池里目标词没有释义时返回 null（交给两问分支）", () => {
    const quiz = buildQuiz("mystery", [
      ...POOL,
      { word: "mystery", pos: null, gloss: "" },
    ]);
    expect(quiz).toBeNull();
  });

  it("词书池不足三个干扰项时，用 mini 库补齐；实在不够才减少选项", () => {
    const quiz = buildQuiz("state", [
      { word: "state", pos: "n.", gloss: "州, 状态" },
      { word: "status", pos: "n.", gloss: "地位" },
    ]);
    expect(quiz).not.toBeNull();
    expect(quiz!.choices.length).toBeGreaterThanOrEqual(2);
    expect(quiz!.choices.some((c) => c.right && c.word === "state")).toBe(true);
  });

  it("不传池时行为与旧版一致（mini 库出题）", () => {
    // complement 是 mini 库里的词
    const quiz = buildQuiz("complement");
    expect(quiz).not.toBeNull();
    expect(quiz!.choices).toHaveLength(4);
  });
});
