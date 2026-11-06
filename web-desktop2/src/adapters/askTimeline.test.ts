import { describe, expect, it } from "vitest";
import {
  emptyTimeline,
  isEmptyTurn,
  reduce,
  replayToTimeline,
  type Timeline,
  type TimelineNode,
} from "./askTimeline";

/** 只取思考块，带上窄化类型 */
const reasonings = (tl: Timeline): Extract<TimelineNode, { kind: "reasoning" }>[] =>
  tl.nodes.filter((n): n is Extract<TimelineNode, { kind: "reasoning" }> => n.kind === "reasoning");

/**
 * session_bound（服务端告知"这一轮写在哪个会话"）是**归属信息**，不是一条可展示的
 * 消息：它进时间线会凭空多出一个空气泡。这里把它锁住——顺带防止以后有人把它
 * 当成节点渲染出来。
 */
describe("reduce · session_bound", () => {
  it("不产生任何节点，也不动已确认边界", () => {
    const tl = reduce(emptyTimeline(), { type: "session_bound", sessionId: "session-1" });

    expect(tl.nodes).toHaveLength(0);
    expect(tl.confirmed).toBe(0);
  });

  it("夹在文字中间也不打断正文", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "text_delta", text: "前半句" });
    tl = reduce(tl, { type: "session_bound", sessionId: "session-1" });
    tl = reduce(tl, { type: "text_delta", text: "后半句" });

    const texts = tl.nodes.flatMap((n) => (n.kind === "text" ? [n.text] : []));
    expect(texts.join("")).toBe("前半句后半句");
  });
});

/**
 * 思考块（阶段 2 思考采集）：思考是展示型内容，与正文并列的一条节点。
 * 顺序即发生顺序；未确认的半截思考随 turn_retry 整块撤掉（与半截正文同命运）。
 */
describe("reduce · reasoning_delta（思考块）", () => {
  it("思考增量累积成一块，正文一到就收成完成态", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "先看" });
    tl = reduce(tl, { type: "reasoning_delta", text: "时态" });

    const [r] = reasonings(tl);
    expect(reasonings(tl)).toHaveLength(1);
    expect(r.text).toBe("先看时态");
    expect(r.done).toBe(false); // 还在想

    tl = reduce(tl, { type: "text_delta", text: "答案是 42" });
    expect(reasonings(tl)[0].done).toBe(true);
    // 顺序即发生顺序：思考在前、正文在后
    expect(tl.nodes.map((n) => n.kind)).toEqual(["reasoning", "text"]);
  });

  it("空文字片段不切断同一段思考", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "前半" });
    tl = reduce(tl, { type: "text_delta", text: "" });
    tl = reduce(tl, { type: "reasoning_delta", text: "后半" });

    expect(reasonings(tl)).toHaveLength(1);
    expect(reasonings(tl)[0].text).toBe("前半后半");
  });

  it("新一轮开始时清掉上一轮没确认的半截思考", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "半截不要了" });
    tl = reduce(tl, { type: "run_started" });
    tl = reduce(tl, { type: "reasoning_delta", text: "新的思考" });

    expect(reasonings(tl)).toHaveLength(1);
    expect(reasonings(tl)[0].text).toBe("新的思考");
  });

  it("记录真实思考时长，不用页面停留时间", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "想一下" }, 1000);
    tl = reduce(tl, { type: "text_delta", text: "答" }, 5000);

    expect(reasonings(tl)[0].durationMs).toBe(4000);
  });

  it("纯工具轮：assistant_message 收完整时思考也收成完成态，且不多出空气泡", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "该查词典" });
    tl = reduce(tl, { type: "assistant_message", text: "" });

    expect(reasonings(tl)[0].done).toBe(true);
    expect(tl.nodes.some((n) => n.kind === "text")).toBe(false);
  });

  it("工具轮之间的两段思考各自成块，不合并", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "第一轮想" });
    tl = reduce(tl, { type: "assistant_message", text: "" });
    tl = reduce(tl, { type: "tool_call", id: "c1", name: "lookup_word" });
    tl = reduce(tl, { type: "reasoning_delta", text: "第二轮想" });

    expect(reasonings(tl).map((n) => n.text)).toEqual(["第一轮想", "第二轮想"]);
  });

  it("turn_retry 撤掉本轮未确认的思考，已确认的不动", () => {
    let tl = emptyTimeline();
    // 第一轮：思考 + 纯工具调用（assistant_message 把思考钉进已确认边界）
    tl = reduce(tl, { type: "reasoning_delta", text: "第一轮已确认" });
    tl = reduce(tl, { type: "assistant_message", text: "" });
    tl = reduce(tl, { type: "tool_call", id: "c1", name: "lookup_word" });
    tl = reduce(tl, { type: "tool_result", id: "c1", ok: true, output: {} });
    // 第二轮：流了半截思考就断流重发
    tl = reduce(tl, { type: "reasoning_delta", text: "半截不要了" });
    tl = reduce(tl, { type: "turn_retry", message: "网络中断" });

    expect(reasonings(tl).map((n) => n.text)).toEqual(["第一轮已确认"]);
  });

  it("用户停止：思考也收成完成态，不会永远转圈", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "reasoning_delta", text: "想了半截" });
    tl = reduce(tl, { type: "result", finishReason: "cancelled" });

    expect(reasonings(tl)[0].done).toBe(true);
  });
});

describe("assistant_message · 整轮合并", () => {
  it("半截文字后面插入思考块，最终消息仍然只保留一条正文", () => {
    let tl = emptyTimeline();
    tl = reduce(tl, { type: "run_started" });
    tl = reduce(tl, { type: "text_delta", text: "你好。" });
    tl = reduce(tl, { type: "reasoning_delta", text: "先判断这是问候。" });
    tl = reduce(tl, { type: "assistant_message", text: "你好。今天有 1 个知识点到了复习时间。" });

    const texts = tl.nodes.flatMap((n) => (n.kind === "text" ? [n.text] : []));
    expect(texts).toEqual(["你好。今天有 1 个知识点到了复习时间。"]);
    expect(reasonings(tl)).toHaveLength(1);
    expect(reasonings(tl)[0].done).toBe(true);
  });
});

describe("replayToTimeline · reasoning（历史回放）", () => {
  it("思考条目还原成已完成的思考块，位置在它解释的答复之前", () => {
    const tl = replayToTimeline([
      { kind: "user", seq: 1, text: "这题选什么" },
      { kind: "reasoning", seq: 2, text: "先判断时态" },
      { kind: "assistant", seq: 3, text: "选 B" },
    ]);

    expect(tl.nodes.map((n) => n.kind)).toEqual(["user", "reasoning", "text"]);
    const [r] = reasonings(tl);
    expect(r.text).toBe("先判断时态");
    expect(r.done).toBe(true); // 落盘过的思考一定是完整的一轮
  });

  it("旧会话没有 reasoning 条目时照常工作（向后兼容）", () => {
    const tl = replayToTimeline([
      { kind: "user", seq: 1, text: "hi" },
      { kind: "assistant", seq: 2, text: "你好" },
    ]);
    expect(tl.nodes.map((n) => n.kind)).toEqual(["user", "text"]);
  });
});

describe("isEmptyTurn · 这一轮是不是什么都没产生", () => {
  const done = (): TimelineNode => ({ id: "t1", kind: "terminal", state: "done", label: "", sortKey: 9 });
  const text = (s: string, id = "x"): TimelineNode => ({ id, kind: "text", text: s, confirmed: true, sortKey: 1 });

  it("还在跑（没有终态）→ 不算空", () => {
    expect(isEmptyTurn([text("半截")].slice(0, 1).concat([]))).toBe(false);
    expect(isEmptyTurn([])).toBe(false);
  });

  it("终态不是 done（出错 / 取消 / 未确认）→ 不算空，那些另有 UI", () => {
    expect(isEmptyTurn([{ ...done(), state: "failed" } as TimelineNode])).toBe(false);
    expect(isEmptyTurn([{ ...done(), state: "cancelled" } as TimelineNode])).toBe(false);
    expect(isEmptyTurn([{ ...done(), state: "unconfirmed" } as TimelineNode])).toBe(false);
  });

  it("正常结束、有正文 → 不算空", () => {
    expect(isEmptyTurn([text("批改结果…"), done()])).toBe(false);
  });

  it("正常结束、正文是空白 → 算空", () => {
    expect(isEmptyTurn([text("   \n  "), done()])).toBe(true);
  });

  it("正常结束、只有终态 → 算空", () => {
    expect(isEmptyTurn([done()])).toBe(true);
  });

  it("有工具结果 / 定稿卡 / 反问卡 → 不算空（那都是给用户看的内容）", () => {
    const tool: TimelineNode = { id: "k", kind: "tool", name: "grade_text", state: "ok", args: {}, output: {}, sortKey: 1 } as TimelineNode;
    expect(isEmptyTurn([tool, done()])).toBe(false);
  });

  it("只有思考、没有正文 → 算空（思考不是给用户的内容）", () => {
    const r: TimelineNode = { id: "r", kind: "reasoning", text: "先看时态…", done: true, sortKey: 1 };
    expect(isEmptyTurn([r, done()])).toBe(true);
  });
});
