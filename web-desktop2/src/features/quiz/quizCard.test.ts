import { describe, it, expect } from "vitest";
import { extractQuiz, singleChoiceLetters, buildReadingAnswers } from "./quizCard";

describe("extractQuiz 归一化 get_quiz 工具结果", () => {
  it("found=false（题库没有这类题）→ null，不硬渲染题卡", () => {
    expect(extractQuiz({ found: false, note: "没有题" })).toBeNull();
    expect(extractQuiz(undefined)).toBeNull();
    expect(extractQuiz({})).toBeNull();
  });

  it("单题卡：只有 body 文本 → single，原样保留题面", () => {
    const q = extractQuiz({
      found: true, quiz_id: "q1", qtype: "语法填空", body: "Choose the best one: A.go B.goes C.going",
    });
    expect(q?.kind).toBe("single");
    if (q?.kind === "single") {
      expect(q.quizId).toBe("q1");
      expect(q.body).toContain("Choose the best one");
    }
  });

  it("阅读整套卡：passage + questions（含结构化选项）→ reading", () => {
    const q = extractQuiz({
      found: true, quiz_id: "r1", qtype: "阅读理解", passage: "PASSAGE TEXT",
      questions: [
        { number: 1, stem: "What is the main idea?", options: { A: "x", B: "y", C: "z", D: "w" } },
      ],
    });
    expect(q?.kind).toBe("reading");
    if (q?.kind === "reading") {
      expect(q.passage).toBe("PASSAGE TEXT");
      expect(q.questions[0].options.A).toBe("x");
      expect(q.questions[0].number).toBe(1);
    }
  });
});

describe("singleChoiceLetters 从单题题面提取可选字母", () => {
  it("识别 A. B. C. D. 四种写法", () => {
    expect(singleChoiceLetters("( ) 1. xxx A. go  B. goes  C. going  D. gone")).toEqual(["A", "B", "C", "D"]);
  });
  it("识别 A) B) C) D) 与中文顿号", () => {
    expect(singleChoiceLetters("x A) a B) b C) c D) d")).toEqual(["A", "B", "C", "D"]);
  });
  it("七选五给 A-G", () => {
    const body = Array.from({ length: 7 }, (_, i) => String.fromCharCode(65 + i) + ". opt").join(" ");
    expect(singleChoiceLetters(body)).toEqual(["A", "B", "C", "D", "E", "F", "G"]);
  });
  it("题面没标出选项时回退 A-D，不返回空", () => {
    expect(singleChoiceLetters("Please answer the question.")).toEqual(["A", "B", "C", "D"]);
  });
});

describe("buildReadingAnswers 拼阅读作答串", () => {
  it("按题号升序拼成 1.X 2.X（后端 check_quiz_answer 的格式）", () => {
    expect(buildReadingAnswers({ 2: "A", 1: "D", 3: "C" })).toBe("1.D 2.A 3.C");
  });
  it("空选择返回空串", () => {
    expect(buildReadingAnswers({})).toBe("");
  });
});
