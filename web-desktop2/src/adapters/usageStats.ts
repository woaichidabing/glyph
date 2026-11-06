/**
 * 设置页「用量与花费」的取数与摆位（纯函数，vitest 直接测）。
 *
 * 口径与对话框那条状态条共用一套（`adapters/usage.ts`）：命中部分按标准价 2 折计费，
 * 所以"省了多少"是按钱算的。唯一多出来的一条规矩：
 *
 * **命中率的分母只认"带了缓存明细"的那部分输入**（`measured_input_tokens`）。
 * 2026-10-12 之前的流水只记了输入、没记 cached_tokens；拿全部输入当分母会把真值 95%
 * 压成 19%，是纯误导。
 */

import { CACHE_SAVED_FACTOR } from "./usage";

export type UsageDay = {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number | null;
  measured_input_tokens: number;
  turns: number;
};

export type UsageSummaryPayload = {
  window_days: number;
  days: UsageDay[];
  total: UsageDay;
};

/** 拉最近 N 天的用量汇总。失败抛人话错误（设置页直接显示）。 */
export async function loadUsageSummary(days = 7): Promise<UsageSummaryPayload> {
  const res = await fetch(`/api/usage/summary?days=${days}`);
  if (!res.ok) throw new Error("暂时取不到用量，稍后再试。");
  return (await res.json()) as UsageSummaryPayload;
}

export type Bar = {
  day: string;
  label: string;
  /** 柱子总高（相对窗口内最大的一天，0-100） */
  totalPct: number;
  /** 其中"命中缓存"那一段的高度（0-100），画成深色 */
  cachedPct: number;
};

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/** "2026-10-12" → "周一"。解析不出来就照原样返回，不编。 */
export function weekdayLabel(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return WEEKDAYS[d.getDay()] ?? iso;
}

/** token 口径的命中率（0-100 整数）；没带明细时 null（未知，不是 0）。 */
export function hitPct(row: UsageDay): number | null {
  if (row.cached_tokens === null || row.measured_input_tokens <= 0) return null;
  return Math.round((row.cached_tokens / row.measured_input_tokens) * 100);
}

/** 按钱算省了多少（0-100 整数）；没带明细时 null。 */
export function savedPct(row: UsageDay): number | null {
  if (row.cached_tokens === null || row.measured_input_tokens <= 0) return null;
  return Math.round((CACHE_SAVED_FACTOR * row.cached_tokens * 100) / row.measured_input_tokens);
}

/** 把每天的量摆成柱子：高度相对窗口内最大的一天，深色段是命中缓存的那部分。 */
export function bars(days: UsageDay[]): Bar[] {
  const max = days.reduce((m, d) => Math.max(m, d.input_tokens), 0) || 1;
  return days.map((d) => ({
    day: d.day,
    label: weekdayLabel(d.day),
    totalPct: Math.round((d.input_tokens / max) * 100),
    cachedPct: Math.round(((d.cached_tokens ?? 0) / max) * 100),
  }));
}
