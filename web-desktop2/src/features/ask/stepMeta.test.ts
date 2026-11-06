import { describe, it, expect } from "vitest";
import { stepMetaOf, stepArgOf, stepInfoOf, stepOutputText } from "./stepMeta";
import type { ToolNode } from "../../adapters/askTimeline";

const node = (over: Partial<ToolNode> = {}): ToolNode => ({
  id: "t1", kind: "tool", callId: "c1", name: "read_file", state: "ok",
  startedAt: 0, sortKey: 0, ...over,
});

describe("stepMeta", () => {
  it("已知工具给中文动词与图标", () => {
    expect(stepMetaOf("grade_text").verb).toBe("批改英文");
    expect(stepMetaOf("lookup_word").icon).toBe("book");
  });

  it("不认识的工具按名字猜个人话，不甩英文", () => {
    expect(stepMetaOf("code_exec").verb).toBe("运行命令");
    expect(stepMetaOf("weird_thing").verb).toBe("调用工具");
  });

  it("短参数只取文件名，不带目录，过长截断", () => {
    expect(stepArgOf(node({ args: { path: "C:/a/b/notes.md" } }))).toBe("notes.md");
    expect(stepArgOf(node({ args: { query: "x".repeat(40) } })).endsWith("…")).toBe(true);
    expect(stepArgOf(node())).toBe("");
  });

  it("右侧信息：运行中/失败/已停止优先，成功才看结果", () => {
    expect(stepInfoOf(node({ state: "running" }))).toBe("运行中");
    expect(stepInfoOf(node({ state: "fail" }))).toBe("失败");
    expect(stepInfoOf(node({ state: "cancelled" }))).toBe("已停止");
    expect(stepInfoOf(node({ output: { match_count: 3 } }))).toBe("3 处匹配");
    expect(stepInfoOf(node({ output: { results: [1, 2] } }))).toBe("2 条结果");
  });

  it("成功且没数字时不写「完成」", () => {
    expect(stepInfoOf(node())).toBe("");
  });

  it("失败优先给错误原因", () => {
    expect(stepOutputText(node({ state: "fail", error: "识别失败" }))).toBe("识别失败");
  });

  it("批改结果说人话：先给计数，再列前三条", () => {
    const text = stepOutputText(
      node({
        name: "grade_text",
        output: {
          detections: [
            { tier: "error", original: "She go", corrected: "She goes" },
            { tier: "suggestion", original: "very like", corrected: "really like" },
            { tier: "error", original: "in 2020 year", corrected: "in 2020" },
            { tier: "error", original: "a", corrected: "an" },
          ],
          stats: { error_count: 3, suggestion_count: 1 },
          summary: "整体表达清楚。",
        },
      }),
    );
    expect(text).toContain("共 4 处");
    expect(text).toContain("硬错 3");
    expect(text).toContain("1) She go → She goes");
    expect(text).toContain("其余 1 条");
    expect(text).toContain("整体表达清楚。");
  });

  it("查词结果说人话：词 + 音标（真实是对象）+ 中文释义（真实在 senses）", () => {
    const text = stepOutputText(
      node({
        name: "lookup_word",
        output: {
          word: "weekend",
          phonetic: { uk: "ˌwiːkˈend", us: "ˈwiːkend" },
          senses: [{ pos: "n.", translation: "周末" }],
        },
      }),
    );
    expect(text).toContain("weekend");
    expect(text).toContain("ˌwiːkˈend");
    expect(text).toContain("n. 周末");
  });

  it("写作评级用真实字段 level_range", () => {
    expect(stepOutputText(node({ name: "analyze_writing", output: { level_range: ["B1", "B2"] } }))).toBe(
      "等级：B1-B2",
    );
  });

  it("认不出的形状也不甩大括号", () => {
    const text = stepOutputText(node({ name: "unknown_tool", output: { a: 1, b: "x" } }));
    expect(text).toContain("a: 1");
    expect(text.startsWith("{")).toBe(false);
  });
});
