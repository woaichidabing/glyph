import { describe, it, expect } from "vitest";
import {
  todayList,
  weekCells,
  monthMatrix,
  monthStats,
  type PlanTask,
} from "./planStats";

/* 计划统计纯函数：全部以传入 now 为准，不依赖当天日期。
   NOW = 2026-10-08 周四；本周一 10-05，周日 10-11；10 月 1 日是周四。 */

const NOW = new Date(2026, 9, 8);
const day = (d: number) => `2026-10-${String(d).padStart(2, "0")}`;

const T = (over: Partial<PlanTask> & Pick<PlanTask, "id" | "t">): PlanTask => ({
  src: "own",
  done: false,
  createdAt: "2026-10-01",
  doneAt: null,
  ...over,
});

describe("todayList · 今天的任务", () => {
  it("未完成的 + 今天完成的；更早完成的不出现", () => {
    const tasks = [
      T({ id: "a", t: "周一完成的", done: true, doneAt: day(5) }),
      T({ id: "b", t: "今天完成的", done: true, doneAt: day(8) }),
      T({ id: "c", t: "还没做" }),
    ];
    const list = todayList(tasks, NOW);
    expect(list.map((t) => t.id)).toEqual(["b", "c"]);
  });

  it("今天没有任何任务时返回空数组（视图走空态）", () => {
    expect(todayList([T({ id: "a", t: "旧任务", done: true, doneAt: "2026-09-30" })], NOW))
      .toEqual([]);
  });
});

describe("weekCells · 本周七格", () => {
  it("过去格数当天完成数；今天给剩余件数；未来为空", () => {
    const tasks = [
      T({ id: "a", t: "周一完成", done: true, doneAt: day(5) }),
      T({ id: "b", t: "今天完成", done: true, doneAt: day(8) }),
      T({ id: "c", t: "未完成" }),
    ];
    const cells = weekCells(tasks, NOW);
    expect(cells.map((c) => c.weekday)).toEqual(["周一", "周二", "周三", "周四", "周五", "周六", "周日"]);
    expect(cells[0]).toMatchObject({ state: "past", date: day(5), done: 1 });
    expect(cells[1]).toMatchObject({ state: "past", done: 0 });
    expect(cells[3]).toMatchObject({ state: "today", date: day(8), done: 1, left: 1 });
    expect(cells[4]).toMatchObject({ state: "future" });
  });

  it("跨周边界：周日为一周最后一天", () => {
    const cells = weekCells([], NOW);
    expect(cells[6].date).toBe(day(11));
  });
});

describe("monthMatrix · 月历", () => {
  it("周一开头留空，完成日打标，今天高亮", () => {
    const tasks = [
      T({ id: "a", t: "5 号完成", done: true, doneAt: day(5) }),
      T({ id: "b", t: "8 号完成", done: true, doneAt: day(8) }),
    ];
    const m = monthMatrix(tasks, NOW);
    const flat = m.cells;
    // 10 月 1 日周四 → 前面 3 个 blank（周一二三）
    expect(flat.slice(0, 3).every((c) => c.state === "blank")).toBe(true);
    const byDay = new Map(flat.filter((c) => c.day > 0).map((c) => [c.day, c.state]));
    expect(byDay.get(1)).toBe("");
    expect(byDay.get(5)).toBe("done");
    expect(byDay.get(8)).toBe("today");
  });
});

describe("monthStats · 本月统计", () => {
  it("本月完成件数与活跃天数（去重日期）；跨月不算", () => {
    const tasks = [
      T({ id: "a", t: "1", done: true, doneAt: day(5) }),
      T({ id: "b", t: "2", done: true, doneAt: day(5) }),
      T({ id: "c", t: "3", done: true, doneAt: day(8) }),
      T({ id: "d", t: "上月", done: true, doneAt: "2026-09-29" }),
      T({ id: "e", t: "未完成" }),
    ];
    const s = monthStats(tasks, NOW);
    expect(s.done).toBe(3);
    expect(s.activeDays).toBe(2);
  });
});
