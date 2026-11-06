/* ============================================================
 * 成长页数据聚合（纯函数，可单测）
 *
 * 数据都是用户自己学出来的真账：
 *   - 已拿下错词 / 查词历史 / 打卡：浏览器 localStorage（各 repo）
 *   - 定稿作文篇数：后端会话 replay 里的 finalize 条目（见 loadFinalizeCounts）
 * 刻意没有「学习分钟」：没有可信计时来源，不编。
 * 四维能力折线依赖批改产出四维分数（大改，后置），在那之前页面显示空态。
 * ============================================================ */
import { readMastered } from "../../adapters/masteredRepo";
import type { Mastered } from "../../adapters/masteredRepo";
import { daysDone, daysDoneInYear, readStreak, todayStr } from "../../adapters/streakRepo";
import type { Streak } from "../../adapters/streakRepo";
import type { HistoryItem } from "../../types";
import { readJSON, writeJSON } from "../../adapters/localStore";
import { getSessionMessagesApi, listSessionsApi } from "../../adapters/sessionApi";

export type Achievement = {
  name: string;
  desc: string;
  now: number;
  need: number;
  /** 达成文案（达成即亮章，本地无历史达成日期，不伪造日期）；null=未达成 */
  at: string | null;
};

export type GrowthInput = {
  mastered: Mastered[];
  history: HistoryItem[];
  streak: Streak;
  daysDoneAllTime: number;
  daysDoneThisYear: number;
  finalizeTotal: number;
  finalizeThisYear: number;
  now?: Date;
};

export type GrowthModel = {
  year: number;
  monthPrefix: string;
  mastered: { total: number; thisMonth: number; words: string[]; more: number };
  totals: { k: string; v: number }[];
  achievements: Achievement[];
  annual: { year: number; cells: { k: string; v: number }[]; foot: string };
};

const SHOW_WORDS = 8;

const p2 = (n: number) => String(n).padStart(2, "0");
const yearPrefix = (d: Date) => String(d.getFullYear());
const monthPrefix = (d: Date) => `${d.getFullYear()}-${p2(d.getMonth() + 1)}`;

function seal(name: string, desc: string, now: number, need: number): Achievement {
  return { name, desc, now: Math.min(now, need), need, at: now >= need ? "已达成" : null };
}

export function buildGrowth(input: GrowthInput): GrowthModel {
  const now = input.now ?? new Date();
  const year = now.getFullYear();
  const mp = monthPrefix(now);

  // 已拿下错词：按日期倒序（同日保持 repo 写入顺序）
  const masteredDesc = [...input.mastered].sort((a, b) => b.date.localeCompare(a.date));
  const masteredTotal = masteredDesc.length;
  const masteredThisMonth = masteredDesc.filter((m) => m.date.startsWith(mp)).length;
  const words = masteredDesc.slice(0, SHOW_WORDS).map((m) => m.word);

  // 查词（历史去重后即不同词的查询记录条数；repo 已按词去重）
  const lookupsTotal = input.history.length;
  const lookupsThisYear = input.history.filter((h) => new Date(h.at).getFullYear() === year).length;
  const masteredThisYear = masteredDesc.filter((m) => m.date.startsWith(yearPrefix(now))).length;

  const achievements: Achievement[] = [
    seal("初来乍到", "完成第一次查词", lookupsTotal, 1),
    seal("开张", "完成第一篇作文定稿", input.finalizeTotal, 1),
    seal("改十篇", "累计定稿 10 篇作文", input.finalizeTotal, 10),
    seal("改五十篇", "累计定稿 50 篇作文", input.finalizeTotal, 50),
    seal("连七", "连续打卡 7 天", input.streak.best, 7),
    seal("连三十", "连续打卡 30 天", input.streak.best, 30),
  ];

  const totals = [
    { k: "查过词", v: lookupsTotal },
    { k: "改过作文", v: input.finalizeTotal },
    { k: "拿下错词", v: masteredTotal },
    { k: "完成天数", v: input.daysDoneAllTime },
  ];

  const cells = [
    { k: "查词", v: lookupsThisYear },
    { k: "批改篇数", v: input.finalizeThisYear },
    { k: "拿下错词", v: masteredThisYear },
    { k: "最长连续", v: input.streak.best },
    { k: "完成天数", v: input.daysDoneThisYear },
  ];

  return {
    year,
    // monthPrefix 供视图「本月」标签使用
    monthPrefix: mp,
    mastered: {
      total: masteredTotal,
      thisMonth: masteredThisMonth,
      words,
      more: Math.max(0, masteredTotal - SHOW_WORDS),
    },
    totals,
    achievements,
    annual: {
      year,
      cells,
      foot: `${year} 年 · 拿下 ${masteredThisYear} 个错词 · 定稿 ${input.finalizeThisYear} 篇作文`,
    },
  };
}

/**
 * 成长页的组装入口：查词历史来自后端快照（store 的 history），其余本地账本照旧。
 *
 * 抽成纯函数是为了让视图只管两件事：ready 了没有、定稿数回来没有——
 * 依赖数组里只放 history / ready，不会因为 setModel 触发自身重跑（无限重渲染）。
 */
export function buildLocalGrowth(
  history: HistoryItem[],
  finalize: Pick<FinalizeCounts, "total" | "thisYear">,
  now: Date = new Date(),
): GrowthModel {
  return buildGrowth({
    mastered: readMastered(),
    history,
    streak: readStreak(),
    daysDoneAllTime: daysDone(),
    daysDoneThisYear: daysDoneInYear(now.getFullYear()),
    finalizeTotal: finalize.total,
    finalizeThisYear: finalize.thisYear,
    now,
  });
}

/* ----------------------------------------------------------
 * 定稿篇数：扫后端会话 replay 里的 finalize 条目。
 * 成长页是低频页，进页面拉一次；当日缓存，后端不可用时诚实降级。
 * ---------------------------------------------------------- */

export type FinalizeCounts = {
  total: number;
  thisYear: number;
  /** 后端不可达（离线/没启动）：视图显示「—」而不是 0 */
  unavailable: boolean;
};

const COUNTS_CACHE_KEY = "agent.growth.finalize.v1";
/** 会话可能很多，只扫最近这些（finalize 都在最近会话里也够用） */
const SCAN_LIMIT = 100;

type CountsCache = { date: string; total: number; thisYear: number };

export async function loadFinalizeCounts(now: Date = new Date()): Promise<FinalizeCounts> {
  const today = todayStr();
  const cached = readJSON<CountsCache | null>(COUNTS_CACHE_KEY, null);
  if (cached && cached.date === today) {
    return { total: cached.total, thisYear: cached.thisYear, unavailable: false };
  }
  try {
    const sessions = await listSessionsApi();
    const year = now.getFullYear();
    let total = 0;
    let thisYear = 0;
    await Promise.all(
      sessions.slice(0, SCAN_LIMIT).map(async (s) => {
        try {
          const m = await getSessionMessagesApi(s.session_id);
          for (const r of m.replay) {
            if (r.kind !== "finalize") continue;
            total += 1;
            const ts = r.ts ? new Date(r.ts).getFullYear() : new Date(s.created_at).getFullYear();
            if (ts === year) thisYear += 1;
          }
        } catch {
          /* 单个会话拉不到就跳过，不影响其它会话统计 */
        }
      }),
    );
    writeJSON(COUNTS_CACHE_KEY, { date: today, total, thisYear });
    return { total, thisYear, unavailable: false };
  } catch {
    return { total: 0, thisYear: 0, unavailable: true };
  }
}
