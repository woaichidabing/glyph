import { describe, it, expect } from "vitest";
import { extractPlan } from "./planCard";

describe("extractPlan", () => {
  it("从 propose_plan 结果解析目标与任务文本", () => {
    const plan = extractPlan({
      proposed: true,
      goal: "三个月过四级",
      tasks: [{ t: "每天背 30 个词" }, { t: "每周写一篇作文" }],
      note: "ignore me",
    });
    expect(plan).toEqual({
      goal: "三个月过四级",
      tasks: ["每天背 30 个词", "每周写一篇作文"],
      phases: [],
      rhythm: {},
    });
  });

  it("容忍任务直接写成字符串、并 trim", () => {
    const plan = extractPlan({
      proposed: true,
      goal: "练好口语",
      tasks: ["  每天跟读 10 分钟  "],
    });
    expect(plan?.tasks).toEqual(["每天跟读 10 分钟"]);
  });

  it("丢弃空白 / 缺 t 的任务项", () => {
    const plan = extractPlan({
      proposed: true,
      goal: "目标",
      tasks: [{ t: "  " }, { t: "做真题" }, { nope: 1 }, "  ", { t: "复盘错题" }],
    });
    expect(plan?.tasks).toEqual(["做真题", "复盘错题"]);
  });

  it("proposed 不为 true 时返回 null", () => {
    expect(extractPlan({ proposed: false, goal: "g", tasks: [{ t: "a" }] })).toBeNull();
  });

  it("目标为空返回 null", () => {
    expect(extractPlan({ proposed: true, goal: "   ", tasks: [{ t: "a" }] })).toBeNull();
  });

  it("没有有效任务返回 null", () => {
    expect(extractPlan({ proposed: true, goal: "g", tasks: [] })).toBeNull();
    expect(extractPlan({ proposed: true, goal: "g", tasks: [{ t: " " }] })).toBeNull();
  });

  it("null / 非对象 / 畸形输入返回 null（不抛错）", () => {
    expect(extractPlan(null)).toBeNull();
    expect(extractPlan(undefined)).toBeNull();
    expect(extractPlan("nope")).toBeNull();
    expect(extractPlan({})).toBeNull();
  });
});


describe("extractPlan · 阶段与周节奏", () => {
  it("解析阶段与七天节奏；非法阶段整条丢掉", () => {
    const plan = extractPlan({
      proposed: true,
      goal: "三个月过四级",
      tasks: ["背词 30 分钟"],
      phases: [
        { fromWeek: 1, toWeek: 4, title: "打底", focus: "高频词 + 听力" },
        { fromWeek: 9, toWeek: 4, title: "顺序反了" },
      ],
      rhythm: { "1": [{ t: "背词 30 分钟" }], "7": [{ t: "复盘错题" }] },
    });
    expect(plan?.phases).toEqual([
      { fromWeek: 1, toWeek: 4, title: "打底", focus: "高频词 + 听力" },
    ]);
    expect(plan?.rhythm).toEqual({
      "1": [{ t: "背词 30 分钟" }],
      "7": [{ t: "复盘错题" }],
    });
  });

  it("没给阶段/节奏时两个字段是空的，不是 undefined", () => {
    const plan = extractPlan({ proposed: true, goal: "目标", tasks: ["做事"] });
    expect(plan?.phases).toEqual([]);
    expect(plan?.rhythm).toEqual({});
  });
});
