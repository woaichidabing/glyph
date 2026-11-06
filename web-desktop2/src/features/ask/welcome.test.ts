import { describe, it, expect } from "vitest";
import { greeting, formatEyebrow, nextOpenTask, latestSession, sessionWhen } from "./welcome";
import type { PlanTask } from "../../adapters/planStats";
import type { SessionSummary } from "../../adapters/sessionApi";

/* 首页空态纯逻辑：时段问候、日期眉标、今日计划摘要、快捷命令。
   时间一律以入参为准，不依赖运行当下。 */

const task = (id: string, done = false, t = id): PlanTask => ({
  id, t, src: "glyph", done, createdAt: "2026-10-10", doneAt: done ? "2026-10-10" : null,
});

describe("greeting 时段问候", () => {
  it.each([0, 3, 5])("深夜 %p 点是关怀句、不带问号", (h) => {
    expect(greeting(h)).toBe("夜深了，注意早点休息。");
  });
  it.each([6, 9, 11])("早上 %p 点", (h) => {
    expect(greeting(h)).toBe("早上好，今天想做点什么？");
  });
  it.each([12, 13])("中午 %p 点", (h) => {
    expect(greeting(h)).toBe("中午好，歇口气再开始。");
  });
  it.each([14, 17])("下午 %p 点", (h) => {
    expect(greeting(h)).toBe("下午好，想从哪件开始？");
  });
  it.each([18, 21, 23])("晚上 %p 点", (h) => {
    expect(greeting(h)).toBe("晚上好，今晚想做点什么？");
  });
});

describe("formatEyebrow 日期眉标", () => {
  it("格式为「M 月 D 日 周X」，月日不补零", () => {
    expect(formatEyebrow(new Date(2026, 9, 10))).toBe("10 月 10 日 周六");
    expect(formatEyebrow(new Date(2026, 0, 1))).toBe("1 月 1 日 周四");
  });
  it("周一也正确", () => {
    expect(formatEyebrow(new Date(2026, 9, 12))).toBe("10 月 12 日 周一");
  });
});

describe("nextOpenTask 下一件未完成", () => {
  it("没有任务返回 null", () => {
    expect(nextOpenTask([])).toBeNull();
  });
  it("全部完成返回 null", () => {
    expect(nextOpenTask([task("a", true), task("b", true)])).toBeNull();
  });
  it("跳过已完成、返回第一件未完成（保持列表顺序）", () => {
    const next = nextOpenTask([task("a", true), task("b", false), task("c", false)]);
    expect(next?.id).toBe("b");
  });
});

const sess = (id: string, created_at: number | string): SessionSummary => ({
  session_id: id, title: id, created_at, is_current: false,
});

/* 「继续上次」：首屏那一行要显示"最近一条会话"。
   首页空态原本的五颗快捷入口（WELCOME_CHIPS）已删 —— 46 条真实用户消息里
   五个前缀命中 0 次，且其中四项侧栏都有更强的专门页面。 */

describe("latestSession 取最近一条会话", () => {
  it("没有会话 → null（首屏那一行整行不显示）", () => {
    expect(latestSession([])).toBeNull();
  });

  it("只有一条就是它", () => {
    expect(latestSession([sess("a", "2026-10-16T21:40:00+08:00")])?.session_id).toBe("a");
  });

  it("多条取 created_at 最大的，跟数组顺序无关", () => {
    const list = [
      sess("old", "2026-10-14T09:00:00+08:00"),
      sess("newest", "2026-10-16T21:40:00+08:00"),
      sess("mid", "2026-10-15T18:00:00+08:00"),
    ];
    expect(latestSession(list)?.session_id).toBe("newest");
    expect(latestSession([...list].reverse())?.session_id).toBe("newest");
  });

  it("created_at 是秒 / 毫秒数字也认（toMs 已归一）", () => {
    const sec = Math.floor(new Date(2026, 9, 16, 21, 40).getTime() / 1000);
    const ms = new Date(2026, 9, 15, 10, 0).getTime();
    expect(latestSession([sess("a", ms), sess("b", sec)])?.session_id).toBe("b");
  });

  it("时间一样时取先遇到的，不来回抖", () => {
    const same = "2026-10-16T21:40:00+08:00";
    expect(latestSession([sess("a", same), sess("b", same)])?.session_id).toBe("a");
  });
});

describe("sessionWhen 会话时间怎么说", () => {
  // 固定"现在" = 本地时间 2026-10-16 22:00，不依赖运行当下，也不受时区影响
  const now = new Date(2026, 9, 16, 22, 0);

  it("今天 → 只给时分", () => {
    expect(sessionWhen(new Date(2026, 9, 16, 21, 40).getTime(), now)).toBe("21:40");
  });

  it("今天凌晨也补零", () => {
    expect(sessionWhen(new Date(2026, 9, 16, 0, 5).getTime(), now)).toBe("00:05");
  });

  it("昨天 → 前缀「昨天」", () => {
    expect(sessionWhen(new Date(2026, 9, 15, 21, 40).getTime(), now)).toBe("昨天 21:40");
  });

  it("前天及更早、今年以内 → 月日", () => {
    expect(sessionWhen(new Date(2026, 9, 14, 9, 0).getTime(), now)).toBe("10 月 14 日");
    expect(sessionWhen(new Date(2026, 0, 3, 9, 0).getTime(), now)).toBe("1 月 3 日");
  });

  it("跨年 → 带上年份", () => {
    expect(sessionWhen(new Date(2025, 11, 31, 23, 0).getTime(), now)).toBe("2025 年 12 月 31 日");
  });

  it("ISO 字符串入参也认", () => {
    // 用本地时间构造 ISO，避免时区把"今天"挪走
    const iso = new Date(2026, 9, 16, 21, 40).toISOString();
    expect(sessionWhen(iso, now)).toBe("21:40");
  });
});
