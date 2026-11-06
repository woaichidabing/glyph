import { describe, it, expect } from "vitest";
import {
  emptyTimeline,
  finalizeUnconfirmed,
  markCancelled,
  reduce,
  replayToTimeline,
  type AskEvent,
  type Timeline,
} from "./askTimeline";
import {
  parseAskArgs,
  resolveAskResult,
  buildSubmission,
  markSubmitting,
  settleSubmitted,
  resetSubmitting,
  type AskNode,
} from "./askCard";

/* ---------------- 测试夹具 ---------------- */

const VALID_ARGS = {
  question: "这篇作文按哪个考试的标准来评分？",
  options: [
    {
      label: "四级 CET-4 (Recommended)",
      description: "按四级 150 分制和常考扣分点评",
    },
    { label: "六级 CET-6", description: "对词汇和句式丰富度要求更高" },
    { label: "先不打分，只改错误", description: "只把语法和表达问题改对" },
  ],
};

const callAsk = (id: string, args: Record<string, unknown> = VALID_ARGS): AskEvent => ({
  type: "tool_call",
  id,
  name: "ask_user",
  args,
});

const resultOf = (
  id: string,
  output: unknown,
  ok = true,
): AskEvent => ({ type: "tool_result", id, ok, output });

function feed(tl: Timeline, ...evs: AskEvent[]): Timeline {
  return evs.reduce((acc, ev) => reduce(acc, ev), tl);
}

const askNodes = (tl: Timeline): AskNode[] =>
  tl.nodes.filter((n): n is AskNode => n.kind === "ask");

/* ---------------- parseAskArgs ---------------- */

describe("parseAskArgs：解析并校验 ask_user 的问题与选项", () => {
  it("正常解析：问题 + 选项说明齐全", () => {
    const p = parseAskArgs(VALID_ARGS);
    expect(p).not.toBeNull();
    expect(p!.question).toBe("这篇作文按哪个考试的标准来评分？");
    expect(p!.options).toHaveLength(3);
    expect(p!.options[0].label).toBe("四级 CET-4");
    expect(p!.options[0].rec).toBe(true);
    expect(p!.options[1].rec).toBe(false);
    expect(p!.options[2].description).toContain("语法");
  });

  it("推荐标记兼容大小写与多余空格，并从展示标签里剥掉", () => {
    const p = parseAskArgs({
      question: "q",
      options: [
        { label: "甲（recommended）", description: "d1" },
        { label: "乙", description: "d2" },
      ],
    });
    // 只认英文契约尾缀 "(Recommended)"；中文括号的不算，避免误伤用户原文
    expect(p!.options[0].rec).toBe(false);
    const p2 = parseAskArgs({
      question: "q",
      options: [
        { label: "  甲  (Recommended) ", description: "d1" },
        { label: "乙", description: "d2" },
      ],
    });
    expect(p2!.options[0].label).toBe("甲");
    expect(p2!.options[0].rec).toBe(true);
  });

  it("description 缺失时补空串，不让界面拿到 undefined", () => {
    const p = parseAskArgs({
      question: "q",
      options: [{ label: "甲" }, { label: "乙" }],
    });
    expect(p!.options.every((o) => typeof o.description === "string")).toBe(true);
  });

  it("问题为空 / 纯空格 → null（等后端 steer 收口，绝不自己编问题）", () => {
    expect(parseAskArgs({ question: "", options: VALID_ARGS.options })).toBeNull();
    expect(parseAskArgs({ question: "   ", options: VALID_ARGS.options })).toBeNull();
  });

  it("选项不是数组 → null", () => {
    expect(parseAskArgs({ question: "q", options: "nope" })).toBeNull();
    expect(parseAskArgs({ question: "q" })).toBeNull();
  });

  it.each([0, 1, 5])("选项数量非法（%s 个）→ null", (n) => {
    const options = Array.from({ length: n }, (_, i) => ({
      label: `选项${i}`,
      description: "d",
    }));
    expect(parseAskArgs({ question: "q", options })).toBeNull();
  });

  it("某个选项 label 缺失或不是字符串 → null", () => {
    expect(
      parseAskArgs({
        question: "q",
        options: [{ description: "没标签" }, { label: "乙", description: "d" }],
      }),
    ).toBeNull();
    expect(
      parseAskArgs({
        question: "q",
        options: [{ label: 3 }, { label: "乙", description: "d" }],
      }),
    ).toBeNull();
  });

  it("args 整体缺失 → null", () => {
    expect(parseAskArgs(undefined)).toBeNull();
  });
});

/* ---------------- resolveAskResult ---------------- */

describe("resolveAskResult：工具结果决定卡片去向", () => {
  it("steer（参数不合规秒回）→ 收卡，不占位", () => {
    const r = resolveAskResult({ status: "steer", summary: "选项至少两个" }, true);
    expect(r.state).toBe("dismissed");
  });

  it("no_answer（没等到回答）→ 收卡", () => {
    const r = resolveAskResult({ status: "no_answer", choice: null, summary: "没等到" }, true);
    expect(r.state).toBe("dismissed");
  });

  it("answered + 选项 → 已答，摘要为所选项", () => {
    const r = resolveAskResult(
      { status: "answered", choice: "四级 CET-4", custom_text: "", summary: "用户选了" },
      true,
    );
    expect(r.state).toBe("answered");
    if (r.state === "answered") {
      expect(r.answer).toBe("四级 CET-4");
      expect(r.kind).toBe("choice");
    }
  });

  it("answered + 自定义文本 → 摘要用用户原文（text 优先）", () => {
    const r = resolveAskResult(
      { status: "answered", choice: null, custom_text: "按雅思 Task 2 来", summary: "" },
      true,
    );
    if (r.state === "answered") {
      expect(r.answer).toBe("按雅思 Task 2 来");
      expect(r.kind).toBe("custom");
    }
  });

  it("结果形态不认识（没 status）/ 工具失败：也必须离开 waiting，不许留死卡", () => {
    expect(resolveAskResult(undefined, true).state).toBe("dismissed");
    expect(resolveAskResult(null, false).state).toBe("dismissed");
    expect(resolveAskResult({ weird: 1 }, true).state).toBe("dismissed");
  });
});

/* ---------------- reducer 集成 ---------------- */

describe("reduce：ask_user 卡片在时间线里的生命周期", () => {
  it("tool_call(ask_user) 生成 waiting 卡片节点，不生成普通工具行", () => {
    const tl = feed(emptyTimeline(), callAsk("c1"));
    const asks = askNodes(tl);
    expect(asks).toHaveLength(1);
    expect(asks[0].state).toBe("waiting");
    expect(asks[0].callId).toBe("c1");
    expect(asks[0].options[0].rec).toBe(true);
    expect(tl.nodes.some((n) => n.kind === "tool")).toBe(false);
  });

  it("参数非法的 tool_call 不显示卡片（dismissed 占位），随后 steer 结果配对收掉", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1", { question: "q", options: [{ label: "只有一个", description: "d" }] }),
      resultOf("c1", { status: "steer", summary: "至少两个选项" }),
    );
    const asks = askNodes(tl);
    expect(asks).toHaveLength(1);
    expect(asks[0].state).toBe("dismissed");
    // 不允许补出普通工具行
    expect(tl.nodes.some((n) => n.kind === "tool")).toBe(false);
  });

  it("用户作答：answered 结果把卡片收成已答态，摘要取后端对账后的值", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      resultOf("c1", {
        status: "answered",
        choice: "六级 CET-6",
        custom_text: "",
        summary: "用户选了",
      }),
    );
    const ask = askNodes(tl)[0];
    expect(ask.state).toBe("answered");
    if (ask.state === "answered") expect(ask.answer).toBe("六级 CET-6");
  });

  it("自定义回答：摘要显示用户原文而不是选项", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      resultOf("c1", {
        status: "answered",
        choice: null,
        custom_text: "按托福独立写作",
        summary: "",
      }),
    );
    const ask = askNodes(tl)[0];
    if (ask.state === "answered") {
      expect(ask.answer).toBe("按托福独立写作");
      expect(ask.answerKind).toBe("custom");
    }
  });

  it("轮次正常结束（result）时仍 waiting 的卡片收掉，不留死卡", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      { type: "result", finishReason: "final_text" },
    );
    expect(askNodes(tl)[0].state).toBe("dismissed");
  });

  it("error 终态同样收掉 waiting 卡片", () => {
    const tl = feed(emptyTimeline(), callAsk("c1"), {
      type: "error",
      message: "模型挂了",
    });
    expect(askNodes(tl)[0].state).toBe("dismissed");
  });

  it("用户点停止：waiting 卡片收掉，已答卡片保留", () => {
    const waiting = feed(emptyTimeline(), callAsk("c1"));
    const stopped = markCancelled(waiting);
    expect(askNodes(stopped)[0].state).toBe("dismissed");

    const answered = feed(
      emptyTimeline(),
      callAsk("c2"),
      resultOf("c2", { status: "answered", choice: "甲", custom_text: "", summary: "" }),
    );
    const stopped2 = markCancelled(answered);
    expect(askNodes(stopped2)[0].state).toBe("answered");
  });

  it("turn_retry 撤掉 waiting 卡片（模型这一轮重来），已答的不动", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      callAsk("c2"),
      resultOf("c1", { status: "answered", choice: "甲", custom_text: "", summary: "" }),
      { type: "turn_retry" },
    );
    const byId = Object.fromEntries(askNodes(tl).map((a) => [a.callId, a.state]));
    expect(byId.c1).toBe("answered");
    // waiting 卡随本轮作废直接移除（重来后模型若再问会发新 call.id 的新卡），与 running 工具一致
    expect(byId.c2).toBeUndefined();
  });

  it("多次反问各自按 callId 配对，不串卡", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      resultOf("c1", { status: "answered", choice: "甲", custom_text: "", summary: "" }),
      callAsk("c2"),
      resultOf("c2", { status: "steer", summary: "bad" }),
    );
    const asks = askNodes(tl);
    expect(asks).toHaveLength(2);
    expect(asks[0].state).toBe("answered");
    expect(asks[1].state).toBe("dismissed");
  });

  it("普通工具的 tool_call/tool_result 不受影响", () => {
    const tl = feed(
      emptyTimeline(),
      { type: "tool_call", id: "g1", name: "grade_text", args: {} },
      resultOf("g1", { ok: true }),
    );
    const tool = tl.nodes.filter((n) => n.kind === "tool");
    expect(tool).toHaveLength(1);
    expect(tool[0].kind === "tool" && tool[0].state).toBe("ok");
  });
});

/* ---------------- 历史回放 ---------------- */

describe("replayToTimeline：历史会话里的 ask_user", () => {
  it("已回答的反问回灌为收合态卡片，带答案摘要", () => {
    const tl = replayToTimeline([
      { kind: "user", seq: 1, text: "帮我改作文" },
      {
        kind: "tool",
        seq: 3,
        call_seq: 2,
        call_id: "c1",
        tool_name: "ask_user",
        ok: true,
        arguments: VALID_ARGS,
        output: {
          status: "answered",
          choice: "四级 CET-4",
          custom_text: "",
          summary: "",
        },
      },
      { kind: "assistant", seq: 4, text: "好，按四级来" },
    ]);
    const asks = askNodes(tl);
    expect(asks).toHaveLength(1);
    expect(asks[0].state).toBe("answered");
    if (asks[0].state === "answered") expect(asks[0].answer).toBe("四级 CET-4");
  });

  it("steer/no_answer 的反问不留任何节点（用户从没见过这一问）", () => {
    const tl = replayToTimeline([
      {
        kind: "tool",
        seq: 1,
        call_id: "c1",
        tool_name: "ask_user",
        ok: true,
        output: { status: "steer", summary: "bad" },
      },
    ]);
    expect(askNodes(tl)).toHaveLength(0);
    expect(tl.nodes.filter((n) => n.kind === "tool")).toHaveLength(0);
  });
});

/* ---------------- 提交答案 ---------------- */

describe("buildSubmission：构造提交载荷（text 优先，空答复不收）", () => {
  const q = VALID_ARGS.question;
  it("点选项：choice 载荷", () => {
    const s = buildSubmission(q, "四级 CET-4", "");
    expect(s).toEqual({ kind: "choice", choice: "四级 CET-4", question: q });
  });
  it("打字与选项同时存在：按 custom text 提交（用户在改需求）", () => {
    const s = buildSubmission(q, "四级 CET-4", "  按雅思 Task 2 ");
    expect(s?.kind).toBe("custom");
    if (s?.kind === "custom") expect(s.text).toBe("按雅思 Task 2");
  });
  it("纯空格文本不抢 choice", () => {
    const s = buildSubmission(q, "六级 CET-6", "   ");
    expect(s?.kind).toBe("choice");
  });
  it("选项和文本都空 → null（不发空请求）", () => {
    expect(buildSubmission(q, "", "")).toBeNull();
    expect(buildSubmission(q, null, "  ")).toBeNull();
  });
});

describe("提交中 / 迟到收卡 / 网络失败回退", () => {
  const waiting = (): AskNode => ({
    id: "ask-c1", kind: "ask", callId: "c1",
    question: "q", options: [], state: "waiting", sortKey: 1,
  });
  it("markSubmitting：waiting 卡打上提交中标记，其余状态原样", () => {
    expect(markSubmitting(waiting()).submitting).toBe(true);
    const answered: AskNode = { ...waiting(), state: "answered", answer: "甲" };
    expect(markSubmitting(answered)).toBe(answered);
  });
  it("settleSubmitted：ok=false（迟到点击）静默收卡；ok=true 保持等待结果", () => {
    const s = markSubmitting(waiting());
    expect(settleSubmitted(s, false).state).toBe("dismissed");
    expect(settleSubmitted(s, true).state).toBe("waiting");
    expect(settleSubmitted(s, true).submitting).toBe(true);
  });
  it("resetSubmitting：网络失败后清掉提交中标记，让用户能重试", () => {
    const s = markSubmitting(waiting());
    const r = resetSubmitting(s);
    expect(r.submitting).toBe(false);
    expect(r.state).toBe("waiting");
  });
  it("已收口（answered/dismissed）的卡片不再被提交结果翻案", () => {
    const answered: AskNode = { ...waiting(), state: "answered", answer: "甲" };
    expect(settleSubmitted(answered, false)).toBe(answered);
    expect(resetSubmitting(answered)).toBe(answered);
    const dismissed: AskNode = { ...waiting(), state: "dismissed" };
    expect(settleSubmitted(dismissed, false)).toBe(dismissed);
  });
});

/* ---------------- 断流 / 新一轮的边界 ---------------- */

describe("ask_user 卡片边界：断流与新一轮", () => {
  it("finalizeUnconfirmed：连接中断没收到终态，waiting 卡收掉（再点也没意义）", () => {
    const tl = feed(emptyTimeline(), callAsk("c1"));
    const next = finalizeUnconfirmed(tl);
    expect(askNodes(next)[0].state).toBe("dismissed");
  });

  it("run_started：等待中又发新消息（旧流被掐），上一轮遗留 waiting 卡收掉", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      { type: "run_started" },
    );
    expect(askNodes(tl)[0].state).toBe("dismissed");
  });

  it("run_started 不碰已答卡片", () => {
    const tl = feed(
      emptyTimeline(),
      callAsk("c1"),
      resultOf("c1", { status: "answered", choice: "甲", custom_text: "", summary: "" }),
      { type: "run_started" },
    );
    expect(askNodes(tl)[0].state).toBe("answered");
  });

  it("queued（排队）不收卡：被排队的是新请求，挂着的卡属于还在跑的任务", () => {
    const tl = feed(emptyTimeline(), callAsk("c1"), { type: "queued" });
    expect(askNodes(tl)[0].state).toBe("waiting");
  });
});
