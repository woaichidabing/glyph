import { describe, it, expect } from "vitest";
import { buildGrowth, buildLocalGrowth, type GrowthInput } from "./growthRepo";
import type { Mastered } from "../../adapters/masteredRepo";
import type { Streak } from "../../adapters/streakRepo";

/* growthRepo 是成长页的纯聚合 seam：本地各账本 + 定稿计数 → 视图模型。
   日期全部由输入的 now 决定，测试不依赖当天日期。 */

const NOW = new Date(2026, 9, 8); // 2026-10-08

function streak(over: Partial<Streak> = {}): Streak {
  return {
    current: 0,
    best: 0,
    last7: [false, false, false, false, false, false, false],
    lastDate: null,
    ...over,
  };
}

function input(over: Partial<GrowthInput> = {}): GrowthInput {
  return {
    mastered: [],
    history: [],
    streak: streak(),
    daysDoneAllTime: 0,
    daysDoneThisYear: 0,
    finalizeTotal: 0,
    finalizeThisYear: 0,
    now: NOW,
    ...over,
  };
}

const M = (word: string, date: string, from?: string): Mastered => ({ word, date, from });

describe("buildGrowth · 已拿下错词", () => {
  it("总数、本月数、最近 8 个词（按日期倒序）与 more", () => {
    const mastered = [
      M("a", "2026-08-01"),
      M("b", "2026-10-02"),
      M("c", "2026-10-07"),
      M("d", "2026-09-30"),
    ];
    const g = buildGrowth(input({ mastered }));
    expect(g.mastered.total).toBe(4);
    expect(g.mastered.thisMonth).toBe(2);
    // 展示最近 8 个，跨月也算，按日期倒序（同日保持写入顺序）
    expect(g.mastered.words).toEqual(["c", "b", "d", "a"]);
    expect(g.mastered.more).toBe(0);
  });

  it("最近掌握超过 8 个时只展示 8 个，more 为剩余数（跨月也算）", () => {
    const mastered = Array.from({ length: 10 }, (_, i) => M(`w${i}`, `2026-10-0${(i % 9) + 1}`));
    const g = buildGrowth(input({ mastered }));
    expect(g.mastered.words).toHaveLength(8);
    expect(g.mastered.more).toBe(2);
  });

  it("同日期按写入顺序稳定排序不去重（repo 已保证幂等）", () => {
    const g = buildGrowth(input({ mastered: [M("x", "2026-10-08"), M("y", "2026-10-08")] }));
    expect(g.mastered.total).toBe(2);
  });
});

describe("buildGrowth · 成就墙（真实计数，达成才亮）", () => {
  it("新用户：六枚章全空，now/need 如实", () => {
    const g = buildGrowth(input());
    expect(g.achievements.every((a) => a.at === null)).toBe(true);
    const first = g.achievements.find((a) => a.name === "初来乍到")!;
    expect(first.now).toBe(0);
    expect(first.need).toBe(1);
  });

  it("查词 1 次亮「初来乍到」；定稿 1 篇亮「开张」", () => {
    const g = buildGrowth(input({
      history: [{ word: "commit", at: Date.parse("2026-10-01") }],
      finalizeTotal: 1,
    }));
    expect(g.achievements.find((a) => a.name === "初来乍到")?.at).not.toBeNull();
    expect(g.achievements.find((a) => a.name === "开张")?.at).not.toBeNull();
  });

  it("定稿 10/50 篇档位；连续打卡 best 达 7/30 档位", () => {
    const g10 = buildGrowth(input({ finalizeTotal: 10, streak: streak({ best: 7 }) }));
    expect(g10.achievements.find((a) => a.name === "改十篇")?.at).not.toBeNull();
    expect(g10.achievements.find((a) => a.name === "改五十篇")?.at).toBeNull();
    expect(g10.achievements.find((a) => a.name === "连七")?.at).not.toBeNull();
    const g50 = buildGrowth(input({ finalizeTotal: 50, streak: streak({ best: 30 }) }));
    expect(g50.achievements.find((a) => a.name === "改五十篇")?.at).not.toBeNull();
    expect(g50.achievements.find((a) => a.name === "连三十")?.at).not.toBeNull();
  });

  it("未达成章显示 now/need 进度", () => {
    const g = buildGrowth(input({ finalizeTotal: 3, streak: streak({ best: 6 }) }));
    const ten = g.achievements.find((a) => a.name === "改十篇")!;
    expect(ten.now).toBe(3);
    expect(ten.need).toBe(10);
    expect(ten.at).toBeNull();
  });
});

describe("buildGrowth · 概览统计与年度档案", () => {
  it("totals 四格：查词、定稿、掌握、完成天数（累计）", () => {
    const g = buildGrowth(input({
      history: [{ word: "a", at: 1 }, { word: "b", at: 2 }],
      finalizeTotal: 3,
      mastered: [M("a", "2026-10-01")],
      daysDoneAllTime: 6,
    }));
    const map = Object.fromEntries(g.totals.map((t) => [t.k, t.v]));
    expect(map).toMatchObject({ 查过词: 2, 改过作文: 3, 拿下错词: 1, 完成天数: 6 });
  });

  it("年度 cells 只数今年：查词按 at、掌握按 date、定稿用 thisYear、完成天数用 thisYear", () => {
    const g = buildGrowth(input({
      history: [
        { word: "old", at: Date.parse("2025-12-31") },
        { word: "new", at: Date.parse("2026-01-02") },
        { word: "new2", at: Date.parse("2026-10-01") },
      ],
      mastered: [M("old", "2025-06-01"), M("new", "2026-05-01")],
      finalizeTotal: 9,
      finalizeThisYear: 4,
      daysDoneThisYear: 11,
      streak: streak({ best: 21 }),
    }));
    const cells = Object.fromEntries(g.annual.cells.map((c) => [c.k, c.v]));
    expect(cells["查词"]).toBe(2);
    expect(cells["拿下错词"]).toBe(1);
    expect(cells["批改篇数"]).toBe(4);
    expect(cells["完成天数"]).toBe(11);
    expect(cells["最长连续"]).toBe(21);
    expect(g.annual.year).toBe(2026);
  });

  it("没有学习分钟这种无数据源的指标", () => {
    const g = buildGrowth(input());
    const keys = [...g.totals.map((t) => t.k), ...g.annual.cells.map((c) => c.k)];
    expect(keys.some((k) => k.includes("分钟"))).toBe(false);
  });
});

describe("buildLocalGrowth · 查词历史改读后端快照", () => {
  it("查过词数量来自传入的 history（store 的后端快照），不是 localStorage", () => {
    const g = buildLocalGrowth(
      [
        { word: "commit", at: Date.parse("2026-10-01"), count: 2 },
        { word: "ambiguous", at: Date.parse("2026-10-02"), count: 1 },
      ],
      { total: 0, thisYear: 0 },
      NOW,
    );
    expect(g.totals.find((t) => t.k === "查过词")?.v).toBe(2);
    // 第一次查词章要亮，证明历史确实被算进成就墙
    expect(g.achievements.find((a) => a.name === "初来乍到")?.at).not.toBeNull();
    expect(g.annual.cells.find((c) => c.k === "查词")?.v).toBe(2);
  });

  it("定稿计数按传入值合并，不被历史覆盖", () => {
    const g = buildLocalGrowth([], { total: 7, thisYear: 3 }, NOW);
    expect(g.totals.find((t) => t.k === "改过作文")?.v).toBe(7);
    expect(g.annual.cells.find((c) => c.k === "批改篇数")?.v).toBe(3);
    expect(g.totals.find((t) => t.k === "查过词")?.v).toBe(0);
  });
});
