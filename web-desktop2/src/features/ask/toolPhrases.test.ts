/**
 * 组头文案拼装（纯函数）的规格测试。
 *
 * 期望值全部来自 `docs/design/ask-transcript-render-design.md` §3（组头文案规则），
 * 不来自实现——实现改了而行为不变，这些用例必须照样过。
 */
import { describe, it, expect } from "vitest";
import { actionPhrase, pastPhrase, toolParam, buildToolGroupTitle ,
  groupElapsedMs,
  elapsedLabel,
} from "./toolPhrases";

describe("actionPhrase：展开态逐项里显示的中文动作（不带「正在」）", () => {
  it("认识的内置工具给出动作短语", () => {
    expect(actionPhrase("grade_text")).toBe("批改英文");
    expect(actionPhrase("lookup_word")).toBe("查词典");
    expect(actionPhrase("web_search")).toBe("联网搜索");
    expect(actionPhrase("ocr_image")).toBe("认图片文字");
  });

  it("不认识的工具退回工具名，不空着（未知工具也要说得出话）", () => {
    expect(actionPhrase("brand_new_tool")).toBe("brand_new_tool");
  });
});

describe("pastPhrase：组头里的过去式短语", () => {
  it("单次调用不带数量", () => {
    expect(pastPhrase("grade_text", 1)).toBe("批改了英文");
    expect(pastPhrase("lookup_word", 1)).toBe("查了词典");
    expect(pastPhrase("read_file", 1)).toBe("读了材料");
  });

  it("多次调用带数量，且各工具的量词不同", () => {
    expect(pastPhrase("grade_text", 2)).toBe("批改了 2 篇英文");
    expect(pastPhrase("ocr_image", 2)).toBe("认了 2 张图的文字");
    expect(pastPhrase("describe_image", 2)).toBe("看了 2 张图");
    expect(pastPhrase("read_file", 3)).toBe("读了 3 份材料");
    expect(pastPhrase("recheck_detection", 2)).toBe("复核了 2 条");
  });

  it("联网搜索的数量是「来源个数」，单次也要说出来", () => {
    expect(pastPhrase("web_search", 1)).toBe("联网搜索了 1 个来源");
    expect(pastPhrase("web_search", 3)).toBe("联网搜索了 3 个来源");
  });

  it("不认识的工具退回动作短语，不空着", () => {
    expect(pastPhrase("brand_new_tool", 1)).toBe("brand_new_tool");
  });
});

describe("buildToolGroupTitle：一整组工具的组头", () => {
  it("空组不渲染（返回 null）", () => {
    expect(buildToolGroupTitle([])).toBeNull();
  });

  it("单个工具成功 → ✓ 过去式", () => {
    expect(buildToolGroupTitle([{ name: "grade_text", state: "ok" }])).toEqual({
      phase: "done",
      text: "批改了英文",
    });
  });

  it("多个工具成功 → 按首次发生顺序，用「，」连接", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "ok" },
        { name: "lookup_word", state: "ok" },
        { name: "web_search", state: "ok", count: 3 },
      ]),
    ).toEqual({ phase: "done", text: "批改了英文，查了词典，联网搜索了 3 个来源" });
  });

  it("同类工具合并计数，不重复念同一个动作", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "ok" },
        { name: "grade_text", state: "ok" },
      ]),
    ).toEqual({ phase: "done", text: "批改了 2 篇英文" });
  });

  it("还在跑 → 进行中，「、」连接 + 省略号", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "running" },
        { name: "lookup_word", state: "running" },
        { name: "web_search", state: "running" },
      ]),
    ).toEqual({ phase: "running", text: "正在批改英文、查词典、联网搜索…" });
  });

  it("只要还有在飞的，整组就算进行中", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "ok" },
        { name: "web_search", state: "running" },
      ]),
    ).toEqual({ phase: "running", text: "正在批改英文、联网搜索…" });
  });

  it("部分失败 → 说清成功的，再用「但」点出失败那步和原因", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "ok" },
        { name: "web_search", state: "fail", failReason: "没连上" },
      ]),
    ).toEqual({ phase: "failed", text: "批改了英文，但联网搜索没连上" });
  });

  it("全部失败 → 逐条说清哪一步没成", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "fail", failReason: "没跑完" },
        { name: "web_search", state: "fail", failReason: "没连上" },
      ]),
    ).toEqual({ phase: "failed", text: "批改英文没跑完，联网搜索没连上" });
  });

  it("用户主动停止 → 中性「已停止」，不渲染成失败", () => {
    expect(
      buildToolGroupTitle([
        { name: "grade_text", state: "ok" },
        { name: "web_search", state: "cancelled" },
      ]),
    ).toEqual({ phase: "cancelled", text: "已停止" });
  });
});

describe("toolParam：展开态逐项里显示的关键参数", () => {
  it("联网搜索给搜索词，查词典给词", () => {
    expect(toolParam("web_search", { query: "virtual reality" })).toBe("virtual reality");
    expect(toolParam("lookup_word", { word: "sustain" })).toBe("sustain");
  });

  it("读文件只给文件名，不甩一整条长路径", () => {
    expect(toolParam("read_file", { path: "C:\\Users\\me\\uploads\\essay.md" })).toBe("essay.md");
    expect(toolParam("read_file", { path: "/tmp/uploads/report.txt" })).toBe("report.txt");
  });

  it("没有关键参数的工具就整个不显示——不显示空 chip，也不编一个", () => {
    expect(toolParam("grade_text", { text: "I go to school." })).toBeUndefined();
    expect(toolParam("web_search", {})).toBeUndefined();
    expect(toolParam("web_search", undefined)).toBeUndefined();
    expect(toolParam("web_search", { query: "   " })).toBeUndefined();
  });

  it("太长的值截断，不许把整行挤爆", () => {
    const got = toolParam("web_search", { query: "a".repeat(200) });
    expect(got).toBeDefined();
    expect(got!.length).toBeLessThanOrEqual(48);
    expect(got!.endsWith("…")).toBe(true);
  });
});

describe("中文说法覆盖：内置工具一个都不许裸奔成英文工具名", () => {
  /**
   * 独立于实现表的清单——与后端注册台账 `src/glyph/tools/assemble.py` 对齐（20 个）。
   * 后端新增工具时，这里与 `toolPhrases.ts` 的表都要补，否则时间线上会冒出英文工具名。
   */
  const BUILTIN = [
    "grade_text",
    "ocr_image",
    "describe_image",
    "web_search",
    "lookup_word",
    "recheck_detection",
    "read_file",
    "read_memory",
    "read_artifact",
    "analyze_writing",
    "polish_text",
    "get_quiz",
    "check_quiz_answer",
    "propose_plan",
    "remember",
    "forget",
    "record_review_result",
    "record_attempt",
    "read_attempts",
    "delete_attempt",
  ];

  for (const name of BUILTIN) {
    it(`${name} 有专属中文说法`, () => {
      const action = actionPhrase(name);
      expect(action, `${name} 缺失中文动作`).not.toBe(name);
      expect(/[\u4e00-\u9fa5]/.test(action)).toBe(true);
      expect(/[\u4e00-\u9fa5]/.test(pastPhrase(name, 1))).toBe(true);
    });
  }
});

/* 整轮计时：工具组头上那句「共 12s」。
   刻意**不靠前端掐表** —— ToolNode 本来就带 startedAt / durationMs，
   用它们算，界面重挂载、切换会话都不会飘。 */
describe("groupElapsedMs · 一整组花了多久", () => {
  const step = (startedAt: number, durationMs: number) => ({ name: "grade_text", state: "ok" as const, startedAt, durationMs });

  it("空组 → null", () => {
    expect(groupElapsedMs([])).toBeNull();
  });

  it("单步：就是它自己的耗时", () => {
    expect(groupElapsedMs([step(1000, 3400)])).toBe(3400);
  });

  it("多步串行：从第一步开始到最后一步结束", () => {
    expect(groupElapsedMs([step(1000, 1200), step(2200, 800), step(3000, 500)])).toBe(2500);
  });

  it("并行重叠：也按最早开始 → 最晚结束算（不是把耗时加起来）", () => {
    expect(groupElapsedMs([step(1000, 5000), step(1200, 5000)])).toBe(5200);
  });

  it("有一步缺耗时（还在跑 / 后端没回）→ null，调用方据此整条不显示", () => {
    expect(groupElapsedMs([step(1000, 1200), { name: "web_search", state: "running" }])).toBeNull();
  });

  it("顺序颠倒也不受影响（谁先落盘不一定谁先跑）", () => {
    expect(groupElapsedMs([step(3000, 500), step(1000, 1200)])).toBe(2500);
  });
});

describe("elapsedLabel · 秒数怎么说", () => {
  it("不到一秒也给 0s，不出现负数或 NaN", () => {
    expect(elapsedLabel(0)).toBe("0s");
    expect(elapsedLabel(-5)).toBe("0s");
    expect(elapsedLabel(400)).toBe("0s");
  });

  it("秒级：四舍五入到整秒", () => {
    expect(elapsedLabel(3400)).toBe("3s");
    expect(elapsedLabel(12600)).toBe("13s");
    expect(elapsedLabel(59400)).toBe("59s");
  });

  it("过了 1 分钟给「Xm Ys」", () => {
    expect(elapsedLabel(60000)).toBe("1m 0s");
    expect(elapsedLabel(63000)).toBe("1m 3s");
    expect(elapsedLabel(125000)).toBe("2m 5s");
  });
});
