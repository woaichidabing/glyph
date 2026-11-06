/* ============================================================
 * 首页空态纯逻辑：时段问候、日期眉标、今日计划摘要、「继续上次」。
 * 不碰 React / localStorage，全部以入参为准，方便单测。
 *
 * 五颗快捷入口（WELCOME_CHIPS）已删：46 条真实用户消息里五个前缀命中 0 次，
 * 且其中四项（查词 / 出题 / 复习 / 规划）侧栏都有更强的专门页面。
 * 原来由 chips 承担的"告诉用户我能干嘛"，改由输入框的占位文字一句话说完。
 * ============================================================ */
import type { PlanTask } from "../../adapters/planStats";
import { toMs, type SessionSummary } from "../../adapters/sessionApi";

/** 时段问候：深夜是一句不带问号的关怀，其余按早 / 午 / 下午 / 晚给一句轻招呼。 */
export function greeting(h: number): string {
  if (h < 6) return "夜深了，注意早点休息。";
  if (h < 12) return "早上好，今天想做点什么？";
  if (h < 14) return "中午好，歇口气再开始。";
  if (h < 18) return "下午好，想从哪件开始？";
  return "晚上好，今晚想做点什么？";
}

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/** 日期眉标：「M 月 D 日 周X」，月日不补零（空格连写，不再用中间点拼接）。 */
export function formatEyebrow(d: Date = new Date()): string {
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 周${WEEKDAYS[d.getDay()]}`;
}

/** 今日列表里的下一件未完成任务（按列表顺序取第一件）；没有则 null。 */
export function nextOpenTask(tasks: PlanTask[]): PlanTask | null {
  return tasks.find((t) => !t.done) ?? null;
}


function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** 本地日历日的零点毫秒（用来算"差几天"，不受时分秒干扰） */
function dayStart(x: Date): number {
  return new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
}

/**
 * 「继续上次」取哪一条：**最近创建的那条会话**，没有就 null（首屏那一行整行不显示）。
 *
 * 按 created_at 比，跟数组顺序无关；时间戳相同就取先遇到的 ——
 * 用严格大于而不是大于等于，避免每次渲染在两三条之间来回抖。
 */
export function latestSession(list: readonly SessionSummary[]): SessionSummary | null {
  let best: SessionSummary | null = null;
  let bestMs = Number.NEGATIVE_INFINITY;
  for (const s of list) {
    const ms = toMs(s.created_at);
    if (ms > bestMs) {
      bestMs = ms;
      best = s;
    }
  }
  return best;
}

/**
 * 会话时间怎么说 —— 只给到"够分清是哪一次"的精度：
 *   今天 → 21:40　　昨天 → 昨天 21:40
 *   今年以内 → 10 月 14 日　　跨年 → 2025 年 12 月 31 日
 *
 * `now` 可传，方便测；不传就是当下。
 */
export function sessionWhen(createdAt: number | string, now: Date = new Date()): string {
  const d = new Date(toMs(createdAt));
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const diffDays = Math.round((dayStart(now) - dayStart(d)) / 86_400_000);
  if (diffDays <= 0) return hm;
  if (diffDays === 1) return `昨天 ${hm}`;
  if (d.getFullYear() === now.getFullYear()) {
    return `${d.getMonth() + 1} 月 ${d.getDate()} 日`;
  }
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`;
}
