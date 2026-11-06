import { describe, expect, it } from "vitest";
import {
  aidAllowed,
  answerKeepsExcludedOff,
  anchorStatus,
  buildExplainPrompt,
  clearQuestion,
  formatClock,
  gapBlanks,
  isAnswered,
  isExpired,
  makeAnchor,
  mapJudgeToReview,
  missingNumbers,
  moveGapOption,
  needsReview,
  notesOfQuestion,
  nthIndexOf,
  optionUsedBy,
  questionIdOfNumber,
  remainingMs,
  renderSegments,
  reviewPriority,
  stateOf,
  structureIssue,
  toggleDoubt,
  toggleInState,
  wholeNotes,
  withAnswer,
  type PassageDetail,
  type QuestionResult,
  type QuestionState,
  type ReadingAttempt,
  type ReadingNote,
  type Structure,
} from "./reading";

const BLOCK_TEXT = "The old view says reading helps, but the old view is out of date.";

function structure(over: Partial<Structure> = {}): Structure {
  return {
    view: "choice",
    blocks: [
      { id: "p1", kind: "prose", text: "First block.", label: "1", blank_no: null, question_id: null, sentence: "" },
      { id: "p2", kind: "prose", text: BLOCK_TEXT, label: "2", blank_no: null, question_id: null, sentence: "" },
    ],
    question_refs: [
      { question_id: "q46", number: 46, blank_no: null, block_id: null },
      { question_id: "q47", number: 47, blank_no: null, block_id: null },
    ],
    options: {},
    picks: [],
    issues: [],
    ...over,
  };
}

function attempt(over: Partial<ReadingAttempt> = {}): ReadingAttempt {
  return {
    attempt_id: "attempt-20261011-090000-abcdef",
    quiz_id: "cet4-2025-06-set1-reading-1",
    stage: "draft",
    qtype: "仔细阅读",
    exam: "四级",
    year: "2025",
    source: "第1套",
    passage_revision: "rev-1",
    mode: "practice",
    session_id: "",
    started_at: "2026-10-11T09:00:00+08:00",
    updated_at: "2026-10-11T09:00:00+08:00",
    submitted_at: null,
    deadline: null,
    timed_out: false,
    questions: [],
    notes: [],
    modes: [],
    aids_used: [],
    results: [],
    submit_no: "",
    source_kind: "live",
    note_source: "",
    ...over,
  };
}

// --------------------------------------------------------------- 段落匹配：按题状态

describe("段落匹配按题候选 / 排除", () => {
  it("同一段落可以被两道题同时选中（第 36 与 39 都选 O）", () => {
    let states: QuestionState[] = [];
    states = withAnswer(states, "q36", "O");
    states = withAnswer(states, "q39", "O");
    expect(stateOf({ questions: states } as ReadingAttempt, "q36").answer).toBe("O");
    expect(stateOf({ questions: states } as ReadingAttempt, "q39").answer).toBe("O");
  });

  it("排除 O 只影响当前题，别的题照旧能选", () => {
    let states: QuestionState[] = [];
    states = toggleInState(states, "q36", "excluded", "O");
    expect(stateOf({ questions: states } as ReadingAttempt, "q36").excluded).toEqual(["O"]);
    expect(stateOf({ questions: states } as ReadingAttempt, "q39").excluded).toEqual([]);
    // 第 36 题排除了，第 39 题仍然能明确选 O
    states = withAnswer(states, "q39", "O");
    expect(stateOf({ questions: states } as ReadingAttempt, "q39").answer).toBe("O");
  });

  it("撤销排除后段可以重新选", () => {
    let states = toggleInState([], "q36", "excluded", "O");
    states = toggleInState(states, "q36", "excluded", "O");
    expect(stateOf({ questions: states } as ReadingAttempt, "q36").excluded).toEqual([]);
  });

  it("明确选入已排除的段会清掉这题的冲突标记", () => {
    const states = toggleInState([], "q36", "excluded", "O");
    const prev = stateOf({ questions: states } as ReadingAttempt, "q36");
    const fixed = answerKeepsExcludedOff(prev, "O");
    expect(fixed.excluded).toEqual([]);
  });

  it("暂定与排除互不串台", () => {
    let states = toggleInState([], "q36", "candidates", "A");
    states = toggleInState(states, "q46", "candidates", "B");
    const s36 = stateOf({ questions: states } as ReadingAttempt, "q36");
    const s46 = stateOf({ questions: states } as ReadingAttempt, "q46");
    expect(s36.candidates).toEqual(["A"]);
    expect(s46.candidates).toEqual(["B"]);
  });

  it("字母集来自题卡：没有 B 就不该出现 B", () => {
    const s = structure({ view: "matching", picks: ["A", "C", "N", "O", "P"] });
    expect(s.picks).toEqual(["A", "C", "N", "O", "P"]);
    expect(s.picks).not.toContain("B");
    expect(s.picks).not.toContain("M");
  });

  it("清空本题回到未答（不用取消勾选假装没答）", () => {
    let states = withAnswer([], "q46", "A");
    states = toggleDoubt(states, "q46");
    states = clearQuestion(states, "q46");
    expect(isAnswered(stateOf({ questions: states } as ReadingAttempt, "q46"))).toBe(false);
    expect(stateOf({ questions: states } as ReadingAttempt, "q46").doubt).toBe(false);
  });
});

// --------------------------------------------------------------- 七选五

describe("七选五：一个句子只用一次", () => {
  it("挪到另一个空要把旧空清空，并说清是从哪个空挪来的", () => {
    let states = withAnswer([], "q1", "A"); // 内部 1 号题 ↔ 原 36 空
    expect(optionUsedBy(states, "A")?.question_id).toBe("q1");

    const moved = moveGapOption(states, "q3", "A");
    expect(optionUsedBy(moved.states, "A")?.question_id).toBe("q3");
    expect(stateOf({ questions: moved.states } as ReadingAttempt, "q1").answer).toBe(""); // 旧空清空
    expect(moved.movedFrom?.question_id).toBe("q1"); // 界面据此「移到别处会清空原空位」
  });

  it("没被用过的句子挪过去不需要确认", () => {
    const states = withAnswer([], "q1", "A");
    const moved = moveGapOption(states, "q5", "G");
    expect(moved.movedFrom).toBeNull();
    expect(stateOf({ questions: moved.states } as ReadingAttempt, "q1").answer).toBe("A");
  });

  it("空位顺序按显示题号，不按块的下标", () => {
    const s = structure({
      view: "gap",
      blocks: [
        { id: "blank-38", kind: "blank", text: "", label: "38", blank_no: 38, question_id: "q3", sentence: "s3" },
        { id: "blank-36", kind: "blank", text: "", label: "36", blank_no: 36, question_id: "q1", sentence: "s1" },
      ],
      question_refs: [
        { question_id: "q1", number: 1, blank_no: 36, block_id: "blank-36" },
        { question_id: "q3", number: 3, blank_no: 38, block_id: "blank-38" },
      ],
    });
    expect(gapBlanks(s).map(b => b.blank_no)).toEqual([36, 38]);
  });
});

// --------------------------------------------------------------- 锚点 / 原句

describe("原文锚点", () => {
  it("同一个词在一段里出现两次：选第二次就记第二次", () => {
    const quote = "old view";
    const first = nthIndexOf(BLOCK_TEXT, quote, 0);
    const second = nthIndexOf(BLOCK_TEXT, quote, 1);
    expect(first).toBeGreaterThanOrEqual(0);
    expect(second).toBeGreaterThan(first);
    expect(nthIndexOf(BLOCK_TEXT, quote, 2)).toBe(-1); // 只有两次

    const a0 = makeAnchor({ blockId: "p2", blockText: BLOCK_TEXT, quote });
    const a1 = makeAnchor({ blockId: "p2", blockText: BLOCK_TEXT, quote, occurrence: 1 });
    expect(a0?.start).toBe(first);
    expect(a1?.start).toBe(second);
    expect(BLOCK_TEXT.slice(a1!.start, a1!.end)).toBe(quote);
    expect(a1).not.toEqual(a0);
  });

  it("选区不在这一段里就不生成锚点（宁可不记，不记错位区间）", () => {
    expect(makeAnchor({ blockId: "p2", blockText: BLOCK_TEXT, quote: "不在这段话里" })).toBeNull();
  });

  it("原句对不上就标待核对，不悄悄挪到相似句子", () => {
    const anchor = makeAnchor({
      blockId: "p2",
      blockText: BLOCK_TEXT,
      quote: "old view",
      revision: "rev-1",
    })!;
    expect(anchorStatus(anchor, BLOCK_TEXT, "rev-1")).toBe("ok");
    expect(anchorStatus(anchor, BLOCK_TEXT, "rev-2")).toBe("stale"); // 题卡换版 → 待核对
    expect(anchorStatus({ ...anchor, start: anchor.start + 1 }, BLOCK_TEXT, "rev-1")).toBe("mismatch");
    expect(anchorStatus(anchor, undefined, "rev-1")).toBe("mismatch");
  });

  it("渲染按偏移量切：两个标记各归各，重叠时用户标记压过证据", () => {
    const text = "abcdefghij";
    const segs = renderSegments(text, [
      { start: 0, end: 3, kind: "word" },
      { start: 6, end: 9, kind: "evidence" },
      { start: 2, end: 7, kind: "line" },
    ]);
    expect(segs.map(s => s.t).join("")).toBe(text);
    // 0-2 只有 word；2-7 被 line 压住（用户自己的划线优先于证据）；7-9 才是纯证据
    expect(segs[0].kind).toBe("word");
    expect(segs.slice(1, 4).map(s => s.kind)).toEqual(["line", "line", "line"]);
    expect(segs[4]).toEqual({ t: "hi", kind: "evidence" });
  });

  it("越界的区间直接丢掉，不把整段染花", () => {
    const segs = renderSegments("abcdef", [{ start: 4, end: 99, kind: "word" }]);
    expect(segs.map(s => s.t).join("")).toBe("abcdef");
    expect(segs.some(s => s.kind === "word")).toBe(false);
  });
});

// --------------------------------------------------------------- 笔记归题

describe("笔记与题目的关联", () => {
  const notes: ReadingNote[] = [
    {
      id: "n1",
      kind: "note",
      question_id: "q46",
      anchor: { passage_revision: "rev-1", block_id: "p2", start: 4, end: 12, quote: "old view" },
      body: "这里是在讲旧观点还是新研究？",
      created_at: "2026-10-11T09:05:00+08:00",
    },
    {
      id: "n2",
      kind: "note",
      question_id: null,
      anchor: null,
      body: "整篇：作者立场偏积极",
      created_at: "2026-10-11T09:06:00+08:00",
    },
  ];

  it("第 2 段便签挂在第 46 题：讲 46 能读到，讲 47 不误读", () => {
    expect(notesOfQuestion(notes, "q46").map(n => n.id)).toEqual(["n1"]);
    expect(notesOfQuestion(notes, "q47")).toEqual([]);
  });

  it("整篇笔记不会随机关联到某一道题", () => {
    expect(wholeNotes(notes).map(n => n.id)).toEqual(["n2"]);
    expect(notesOfQuestion(notes, "q46").map(n => n.id)).toEqual(["n1"]);
  });

  it("自由便签没有原句也能存：body 与原句是两件事", () => {
    const note: ReadingNote = { ...notes[0], anchor: null, body: "我没找到依据，但觉得选 C" };
    expect(note.body).toBe("我没找到依据，但觉得选 C");
    expect(note.anchor).toBeNull();
    // 改想法不动原句：替换 body 保留 anchor
    const edited = { ...note, body: "再想想还是选 C" };
    expect(edited.anchor).toBeNull();
    expect(edited.body).toBe("再想想还是选 C");
  });
});

// --------------------------------------------------------------- 漏答 / 优先级 / 模式

describe("漏答提醒与复盘优先级", () => {
  it("漏答按具体题号报", () => {
    const s = structure();
    const a = attempt({ questions: [{ question_id: "q46", answer: "A", doubt: false, candidates: [], excluded: [] }] });
    expect(missingNumbers(s, a)).toEqual([47]);
  });

  it("优先级：漏答 > 答错 > 答对但存疑 > 有笔记 > 普通答对", () => {
    const base = {
      question_id: "q1",
      number: 1,
      stem: "",
      options: {},
      state: stateOf({ questions: [] } as unknown as ReadingAttempt, "q1"),
      noteCount: 0,
    };
    const result = (over: Partial<QuestionResult>): QuestionResult => ({
      question_id: "q1", number: 1, correct: true, correct_answer: "A", your_answer: "A", ...over,
    });
    const missed = { ...base, result: result({ your_answer: null, correct: false }) };
    const wrong = { ...base, result: result({ correct: false, your_answer: "B" }) };
    const doubted = { ...base, result: result({}), state: { ...base.state, doubt: true } };
    const noted = { ...base, result: result({}), noteCount: 1 };
    const plain = { ...base, result: result({}) };

    expect(reviewPriority(wrong)).toBeLessThan(reviewPriority(missed));
    expect(reviewPriority(missed)).toBeLessThan(reviewPriority(doubted));
    expect(reviewPriority(doubted)).toBeLessThan(reviewPriority(noted));
    expect(reviewPriority(noted)).toBeLessThan(reviewPriority(plain));
    expect(needsReview(plain)).toBe(false);
    expect(needsReview(doubted)).toBe(true);
  });

  it("模拟模式关掉全部语言辅助；练习模式才给", () => {
    expect(aidAllowed(attempt({ mode: "exam" }))).toBe(false);
    expect(aidAllowed(attempt({ mode: "practice" }))).toBe(true);
  });

  it("计时按绝对截止算：换设备时间、刷新都不影响", () => {
    const deadline = "2026-10-11T09:10:00+08:00";
    expect(remainingMs(deadline, Date.parse("2026-10-11T09:07:30+08:00"))).toBe(150_000);
    expect(formatClock(150_000)).toBe("02:30");
    expect(isExpired(deadline, Date.parse("2026-10-11T09:10:00+08:00"))).toBe(true);
    expect(remainingMs(null, 0)).toBeNull();
  });
});

// --------------------------------------------------------------- 结构 / 题目身份

describe("结构与题目身份", () => {
  it("题显示的号和身份不是一回事", () => {
    expect(questionIdOfNumber(structure(), 46)).toBe("q46");
    expect(structureIssue(structure())).toBe("");
    expect(structureIssue(structure({ issues: ["题卡结构需检查"] }))).toBe("题卡结构需检查");
    expect(structureIssue(structure({ view: "unsupported" }))).toContain("暂时");
  });

  it("判卷结果补齐题干与漏答标记", () => {
    const out = mapJudgeToReview(
      [
        { question_id: "q47", number: 47, correct: false, correct_answer: "C", your_answer: null },
        { question_id: "q46", number: 46, correct: true, correct_answer: "A", your_answer: "A" },
      ],
      [
        { number: 46, stem: "Q46", options: { A: "a" } },
        { number: 47, stem: "Q47", options: {} },
      ],
    );
    expect(out.map(r => r.number)).toEqual([46, 47]);
    expect(out[0].stem).toBe("Q46");
    expect(out[1].missed).toBe(true);
  });
});

// --------------------------------------------------------------- 讲题上下文

describe("讲题提示词", () => {
  const detail: PassageDetail = {
    quiz_id: "cet4-2025-06-set1-reading-1",
    qtype: "仔细阅读",
    exam: "四级",
    year: "2025",
    source: "第1套",
    question_count: 1,
    passage: BLOCK_TEXT,
    questions: [{ number: 46, stem: "What does the author mean?", options: { A: "a", B: "b" } }],
    structure: structure({
      question_refs: [{ question_id: "q46", number: 46, blank_no: null, block_id: "p2" }],
    }),
  };

  it("带上哪一篇 / 哪一次 / 哪一题 / 我的作答 / 我的笔记", () => {
    const a = attempt({
      questions: [{ question_id: "q46", answer: "B", doubt: true, candidates: [], excluded: ["D"] }],
      notes: [
        {
          id: "n1",
          kind: "note",
          question_id: "q46",
          anchor: { passage_revision: "rev-1", block_id: "p2", start: 4, end: 12, quote: "old view" },
          body: "我看不懂这个转折",
          created_at: "2026-10-11T09:05:00+08:00",
        },
      ],
      results: [{ question_id: "q46", number: 46, correct: false, correct_answer: "A", your_answer: "B" }],
    });
    const prompt = buildExplainPrompt({ attempt: a, detail, question_id: "q46" });
    expect(prompt).toContain("cet4-2025-06-set1-reading-1");
    expect(prompt).toContain("attempt-20261011-090000-abcdef");
    expect(prompt).toContain("q46");
    expect(prompt).toContain("What does the author mean?");
    expect(prompt).toContain("B");
    expect(prompt).toContain("B（答错）");
    expect(prompt).toContain("我看不懂这个转折");
    expect(prompt).toContain("old view"); // 我的笔记原句
    expect(prompt).toContain("我看不懂这个转折");
    expect(prompt).toContain("D"); // 我排除过什么
    expect(prompt).toContain("不知道就说不知道"); // 反谄媚
  });

  it("漏答时如实说漏答，不替用户找借口", () => {
    const a = attempt({ results: [{ question_id: "q46", number: 46, correct: false, correct_answer: "A", your_answer: null }] });
    const prompt = buildExplainPrompt({ attempt: a, detail, question_id: "q46" });
    expect(prompt).toContain("漏答");
  });
});
