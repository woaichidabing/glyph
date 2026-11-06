/* ============================================================
 * 计划任务本地持久化（localStorage）。
 * 纯统计在 planStats.ts；本文件只管读写与增删改。
 * 长期目标（三个月过六级那种）由后续「让 Glyph 规划」能力写入，
 * 这里的 src 字段已经预留 "markr"。
 * ============================================================ */
import { readJSON, writeJSON } from "./localStore";
import { todayStr } from "./streakRepo";
import type { PlanTask } from "./planStats";

const KEY = "agent.plan.tasks.v1";

export function loadTasks(): PlanTask[] {
  const all = readJSON<PlanTask[]>(KEY, []);
  // 兼容旧版本（无 id / 日期字段的任务）：补字段，不丢用户数据
  return all.map((t, i) => ({
    id: t.id || `mig-${Date.now()}-${i}`,
    t: t.t,
    src: t.src,
    done: !!t.done,
    createdAt: t.createdAt || todayStr(),
    doneAt: t.doneAt ?? null,
  }));
}

export function saveTasks(tasks: PlanTask[]): void {
  writeJSON(KEY, tasks);
}

/** 旧本机任务搬进后端后清掉，避免下次重复迁移。 */
export function clearLegacyTasks(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* localStorage 不可用就当没迁移过；后端 import 按 id 幂等，不会重复 */
  }
}

function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export function addTask(tasks: PlanTask[], text: string, src: PlanTask["src"] = "own"): PlanTask[] {
  const t = text.trim();
  if (!t) return tasks;
  const next = [...tasks, {
    id: newId(), t, src, done: false, createdAt: todayStr(), doneAt: null,
  }];
  saveTasks(next);
  return next;
}

/**
 * 批量加入任务（「让 Glyph 规划」一次收下多条）：一次写盘，来源统一标 glyph。
 * 空白条目自动丢弃；没有有效条目时原样返回、不写盘。
 */
export function addTasks(
  tasks: PlanTask[],
  texts: string[],
  src: PlanTask["src"] = "own",
): PlanTask[] {
  const valid = texts.map((x) => x.trim()).filter(Boolean);
  if (valid.length === 0) return tasks;
  const today = todayStr();
  const next = [
    ...tasks,
    ...valid.map((t) => ({
      id: newId(), t, src, done: false, createdAt: today, doneAt: null as string | null,
    })),
  ];
  saveTasks(next);
  return next;
}

/** 勾选/取消勾选；取消时清掉完成日期，让它回到待办 */
export function toggleTask(tasks: PlanTask[], id: string): PlanTask[] {
  const next = tasks.map((t) => {
    if (t.id !== id) return t;
    const done = !t.done;
    return { ...t, done, doneAt: done ? todayStr() : null };
  });
  saveTasks(next);
  return next;
}

export function removeTask(tasks: PlanTask[], id: string): PlanTask[] {
  const next = tasks.filter((t) => t.id !== id);
  saveTasks(next);
  return next;
}
