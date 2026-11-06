import { readJSON, writeJSON } from "./localStore";

/** 连续打卡：只存日期，不存时长，统计时才去 timerRepo 聚合 */
export type Streak = {
  current: number;
  best: number;
  /** 最近 7 天是否完成，索引 0 = 六天前，6 = 今天 */
  last7: boolean[];
  lastDate: string | null;
};

const KEY = "agent.streak.v1";

function today(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function dayOffset(base: Date, days: number): string {
  const d = new Date(base.getTime());
  d.setDate(d.getDate() + days);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

const EMPTY: Streak = { current: 0, best: 0, last7: [false, false, false, false, false, false, false], lastDate: null };

export function readStreak(): Streak {
  return readJSON<Streak>(KEY, EMPTY);
}

/** 打卡（幂等：同一天重复调用不累加） */
export function checkIn(date: string = today()): Streak {
  const s = readStreak();
  if (s.lastDate === date) return s;

  const base = new Date();
  const yesterday = dayOffset(base, -1);
  const current = s.lastDate === yesterday ? s.current + 1 : 1;
  const last7: boolean[] = [];
  for (let i = -6; i <= 0; i++) {
    const d = dayOffset(base, i);
    last7.push(d === date ? true : readDay(d));
  }
  const next: Streak = { current, best: Math.max(s.best, current), last7, lastDate: date };
  writeJSON(KEY, next);
  writeJSON(dayKey(date), true);
  return next;
}

function dayKey(d: string) {
  return `agent.day.v1:${d}`;
}

function readDay(d: string): boolean {
  return readJSON<boolean>(dayKey(d), false);
}

/**
 * 标记某日完成（日历上的完成态也走这里）。
 *
 * 撤销必须是可逆的：取消勾选之后，连击得从打卡记录里重算。
 * 之前这里只是把当天写 false 就直接返回，current 还挂着旧值——
 * 于是"今天全清了 → 手滑取消 → 再勾回来"会让连击虚涨一截。
 */
export function markDay(date: string, done: boolean): Streak {
  writeJSON(dayKey(date), done);
  if (done) return checkIn(date);
  return recompute();
}

/** 所有打卡过的日期（升序） */
export function doneDays(): string[] {
  const out: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith("agent.day.v1:")) continue;
      if (readJSON<boolean>(k, false)) out.push(k.slice("agent.day.v1:".length));
    }
  } catch {
    /* 隐私模式下读不到，按没有算 */
  }
  return out.sort();
}

export function daysDone(): number {
  return doneDays().length;
}

export function daysDoneInYear(year: number): number {
  const y = String(year);
  return doneDays().filter((d) => d.startsWith(y)).length;
}

function shift(date: string, n: number): string {
  const [yy, mm, dd] = date.split("-").map(Number);
  return dayOffset(new Date(yy, mm - 1, dd), n);
}

/** 从打卡记录反推连击：撤销打卡后用它把状态拨回真实值 */
function recompute(): Streak {
  const days = doneDays();
  const set = new Set(days);
  const now = new Date();
  const t = today();

  let current = 0;
  let cursor = set.has(t) ? t : dayOffset(now, -1);
  while (set.has(cursor)) {
    current += 1;
    cursor = shift(cursor, -1);
  }

  let best = 0;
  let run = 0;
  let prev: string | null = null;
  for (const d of days) {
    run = prev !== null && shift(prev, 1) === d ? run + 1 : 1;
    if (run > best) best = run;
    prev = d;
  }

  const last7: boolean[] = [];
  for (let i = -6; i <= 0; i++) last7.push(set.has(dayOffset(now, i)));

  const stored = readStreak();
  const next: Streak = {
    current,
    // 历史最好不因一次撤销而回落：那是一段真实发生过的记录
    best: Math.max(best, stored.best),
    last7,
    lastDate: days.filter((d) => d <= t).slice(-1)[0] ?? null,
  };
  writeJSON(KEY, next);
  return next;
}

export function todayStr(): string {
  return today();
}
