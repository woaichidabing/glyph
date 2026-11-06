/**
 * 渲染分组的规格测试。
 *
 * 2026-10-31 改口径：从「相邻工具合成一组」改成「一整轮一个过程块」——
 * 文字、思考、工具都归进同块，只有**最后一个动作之后**的那段文字作为交付单独露出。
 * 分组只发生在渲染层，时间线扁平序列与 confirmed 边界不受影响。
 */
import { describe, it, expect } from "vitest";
import { toRenderUnits, type RenderUnit } from "./toolGroups";
import type { TimelineNode } from "../../adapters/askTimeline";

const tool = (id: string, name = "grade_text"): TimelineNode => ({
  id, kind: "tool", callId: id, name, state: "ok", startedAt: 0, sortKey: 0,
});
const text = (id: string): TimelineNode => ({ id, kind: "text", text: id, confirmed: true, sortKey: 0 });
const user = (id: string): TimelineNode => ({ id, kind: "user", text: id, attachments: [], sortKey: 0 });
const reasoning = (id: string): TimelineNode => ({ id, kind: "reasoning", text: id, done: true, sortKey: 0 });

/** 压成可读签名，避免断言里堆结构 */
const shape = (units: RenderUnit[]): string[] =>
  units.map((u) =>
    u.kind === "node"
      ? u.node.kind + ":" + u.node.id
      : u.kind + ":" + u.nodes.map((n) => n.id).join("+"),
  );

describe("toRenderUnits：时间线 → 渲染单元", () => {
  it("空时间线不产生任何单元", () => {
    expect(shape(toRenderUnits([]))).toEqual([]);
  });

  it("用户消息原样成节", () => {
    expect(shape(toRenderUnits([user("u1")]))).toEqual(["user:u1"]);
  });

  it("单个工具进过程块", () => {
    expect(shape(toRenderUnits([tool("a")]))).toEqual(["process:a"]);
  });

  it("同一轮多个工具合成一个过程块，顺序保持发生顺序", () => {
    expect(shape(toRenderUnits([tool("a"), tool("b"), tool("c")]))).toEqual(["process:a+b+c"]);
  });

  it("字后面还有工具，说明只是旁白，收进过程块", () => {
    expect(shape(toRenderUnits([tool("a"), text("t1"), tool("b")]))).toEqual(["process:a+t1+b"]);
  });

  it("最后一个动作之后的文字是交付，露在过程块外", () => {
    expect(shape(toRenderUnits([tool("a"), text("t1"), tool("b"), text("t2")]))).toEqual([
      "process:a+t1+b",
      "text:t2",
    ]);
  });

  it("只有文字没有动作时，整段文字就是交付", () => {
    expect(shape(toRenderUnits([text("t1")]))).toEqual(["text:t1"]);
  });

  it("思考与工具同属一轮，进同一个过程块", () => {
    expect(shape(toRenderUnits([tool("a"), reasoning("r1"), tool("b")]))).toEqual([
      "process:a+r1+b",
    ]);
  });

  it("两条用户消息之间是两轮，各自一个过程块", () => {
    expect(shape(toRenderUnits([user("u1"), tool("a"), user("u2"), tool("b")]))).toEqual([
      "user:u1", "process:a", "user:u2", "process:b",
    ]);
  });
});

describe("挂卡片的工具不并入过程块", () => {
  it("get_quiz 单独露出，并把两侧切成两个过程块", () => {
    expect(shape(toRenderUnits([tool("a"), tool("q", "get_quiz"), tool("b")]))).toEqual([
      "process:a", "tool:q", "process:b",
    ]);
  });

  it("propose_plan 同理", () => {
    expect(shape(toRenderUnits([tool("p", "propose_plan"), tool("b")]))).toEqual([
      "tool:p", "process:b",
    ]);
  });

  it("两个卡片类工具相邻也不归组", () => {
    expect(shape(toRenderUnits([tool("q1", "get_quiz"), tool("q2", "get_quiz")]))).toEqual([
      "tool:q1", "tool:q2",
    ]);
  });

  it("propose_wordbook_add 单独露出", () => {
    expect(shape(toRenderUnits([tool("w", "propose_wordbook_add"), tool("b")]))).toEqual([
      "tool:w", "process:b",
    ]);
  });
});

describe("过程块 id 稳定（不随节点增删变化，否则整块重建会闪）", () => {
  const idOf = (units: RenderUnit[]) => units.filter((u) => u.kind === "process").map((u) => u.id);

  it("同一轮里追加思考/工具，id 不变", () => {
    const base = [user("u1"), tool("a")];
    const more = [user("u1"), tool("a"), reasoning("r1"), tool("b")];
    expect(idOf(toRenderUnits(base))).toEqual(["p-u1-0"]);
    expect(idOf(toRenderUnits(more))).toEqual(["p-u1-0"]);
  });

  it("思考先到、工具后到（第二个思考）也不会换 id", () => {
    expect(idOf(toRenderUnits([user("u1"), reasoning("r1")]))).toEqual(["p-u1-0"]);
    expect(idOf(toRenderUnits([user("u1"), reasoning("r1"), reasoning("r2")]))).toEqual(["p-u1-0"]);
  });

  it("同一轮被卡片切成两段时，两段 id 不同（否则 key 撞车）", () => {
    const ids = idOf(toRenderUnits([user("u1"), tool("a"), tool("q", "get_quiz"), tool("b")]));
    expect(ids).toEqual(["p-u1-0", "p-u1-1"]);
  });

  it("不同轮各自一个 id", () => {
    expect(idOf(toRenderUnits([user("u1"), tool("a"), user("u2"), tool("b")]))).toEqual([
      "p-u1-0",
      "p-u2-0",
    ]);
  });
});
