/* ============================================================
 * 计划任务：类型 + 统计纯函数（不碰 localStorage，可单测）
 * 持久化在 planRepo.ts；store 负责把两者接起来。
 *
 * 任务没有截止日期（dueDate）——学习任务是"今天的事"，漏了就还在
 * 未完成列表里，不做"逾期"焦虑设计。周/月统计只认真实完成日期 doneAt。
 * ============================================================ */

export type PlanTask = {
  id: string;
  t: string;
  src: "glyph" | "own";
  done: boolean;
  /** 创建日期 YYYY-MM-DD */
  createdAt: string;
  /** 完成日期 YYYY-MM-DD；未完成为 null */
  doneAt: string | null;
  /** 完成时间戳（毫秒），计划页显示“09:12”用；旧数据可能没有 */
  doneAtMs?: number | null;
  /** 任务类型：背词 / 查词 / 自定义；旧数据可能没有 */
  kind?: "study" | "dict" | "custom";
  /** 自动完成需要的目标次数；默认 1 */
  target?: number;
  /** 自动完成进度 */
  progress?: number;
  /** 指定日期 YYYY-MM-DD；空表示“今天的任务” */
  scheduledFor?: string | null;
  /** 自动完成回写的证据，例如“查过 commit” */
  evidence?: string | null;
};

export type WeekCell = {
  date: string;
  weekday: string;
  label: string; // 日（几号）
  state: "past" | "today" | "future";
  /** 当天完成的任务数 */
  done: number;
  /** 仅今天：当前剩余未完成件数 */
  left?: number;
};

export type MonthCell = { day: number; state: "blank" | "done" | "today" | "" };

export type MonthStats = { done: number; activeDays: number };

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

export const p2 = (n: number) => String(n).padStart(2, "0");

export function dayKey(d: Date): string {
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
}

/** 未完成任务（今天的待办）+ 今天刚完成的；更早完成的进周/月视图看 */
export function todayList(tasks: PlanTask[], now: Date = new Date()): PlanTask[] {
  const today = dayKey(now);
  return tasks.filter((t) => !t.done || t.doneAt === today);
}

export function weekCells(tasks: PlanTask[], now: Date = new Date()): WeekCell[] {
  const monday = new Date(now);
  monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
  const today = dayKey(now);
  const leftCount = tasks.filter((t) => !t.done).length;
  return WEEKDAYS.map((weekday, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    const key = dayKey(d);
    const done = tasks.filter((t) => t.doneAt === key).length;
    const state: WeekCell["state"] =
      key < today ? "past" : key === today ? "today" : "future";
    return {
      date: key,
      weekday,
      label: String(d.getDate()),
      state,
      done,
      ...(state === "today" ? { left: leftCount } : {}),
    };
  });
}

export function monthMatrix(tasks: PlanTask[], now: Date = new Date()): { cells: MonthCell[] } {
  const year = now.getFullYear();
  const month = now.getMonth();
  const offset = (new Date(year, month, 1).getDay() + 6) % 7; // 周一开头的前置空格
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const doneSet = new Set(
    tasks
      .filter((t) => t.doneAt && t.doneAt.startsWith(`${year}-${p2(month + 1)}`))
      .map((t) => t.doneAt as string),
  );
  const today = dayKey(now);
  const cells: MonthCell[] = [];
  for (let i = 0; i < offset; i += 1) cells.push({ day: 0, state: "blank" });
  for (let d = 1; d <= daysInMonth; d += 1) {
    const key = `${year}-${p2(month + 1)}-${p2(d)}`;
    const state: MonthCell["state"] =
      key === today ? "today" : doneSet.has(key) ? "done" : "";
    cells.push({ day: d, state });
  }
  return { cells };
}

export function monthStats(tasks: PlanTask[], now: Date = new Date()): MonthStats {
  const prefix = `${now.getFullYear()}-${p2(now.getMonth() + 1)}`;
  const doneTasks = tasks.filter((t) => t.doneAt?.startsWith(prefix));
  return {
    done: doneTasks.length,
    activeDays: new Set(doneTasks.map((t) => t.doneAt)).size,
  };
}
