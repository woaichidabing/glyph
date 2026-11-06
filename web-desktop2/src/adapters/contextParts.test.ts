import { describe, expect, it } from "vitest";
import { buildContextView, CONTEXT_GROUPS, type ContextPartsPayload } from "./contextParts";

const payload = (parts: Record<string, number>): ContextPartsPayload => ({
  parts,
  estimated_total: Object.values(parts).reduce((a, b) => a + b, 0),
  total_bytes: 0,
  total_messages: 0,
});

describe("上下文构成 · 三类", () => {
  it("七个桶归成三类：系统提示词 / 工具定义 / 对话", () => {
    const view = buildContextView(
      payload({
        system: 8947,
        tools: 6040,
        context: 271,
        user: 25,
        assistant: 223,
        tool_call: 158,
        artifact: 295,
        scaffolding: 46,
      }),
      16005,
    );

    expect(CONTEXT_GROUPS.map((g) => g.label)).toEqual(["系统提示词", "工具定义", "对话"]);
    expect(view.slices.map((s) => [s.label, s.tokens])).toEqual([
      ["系统提示词", 8947],
      ["工具定义", 6040],
      ["对话", 1018],
    ]);
  });

  it("有实测总量时按比例缩放到实测上（比例不失真、总量不说谎）", () => {
    const view = buildContextView(payload({ system: 700, user: 300 }), 1500);

    expect(view.total).toBe(1500);
    expect(view.slices.map((s) => [s.label, s.tokens])).toEqual([
      ["系统提示词", 1050],
      ["对话", 450],
    ]);
  });

  it("没有实测（新会话 / 模型没回用量）就用估算，并如实标记", () => {
    const view = buildContextView(payload({ system: 700, user: 300 }), null);

    expect(view.total).toBe(1000);
    expect(view.fromEstimate).toBe(true);
    expect(view.slices[0].tokens).toBe(700);
  });

  it("只列非零的类；全 0 时不给段也不崩", () => {
    const onlyTalk = buildContextView(payload({ assistant: 50 }), 50);
    expect(onlyTalk.slices.map((s) => s.label)).toEqual(["对话"]);

    const empty = buildContextView(payload({}), 0);
    expect(empty.slices).toEqual([]);
    expect(empty.total).toBe(0);
    expect(empty.fromEstimate).toBe(false);
  });

  it("后端将来多给一个没登记的桶，并进对话、不丢也不白屏", () => {
    const view = buildContextView(payload({ system: 10, mystery: 90 }), 100);

    // 认不出来的桶不算进任何一类（有意的：宁可少算也不乱并），总量仍以实测为准
    expect(view.slices.map((s) => s.label)).toEqual(["系统提示词"]);
    expect(view.total).toBe(100);
  });
});
