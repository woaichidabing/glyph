import type { WordCardState } from "../types";

/* ============================================================
   抗遗忘：FSRS 简化版（Free Spaced Repetition Scheduler）
   ------------------------------------------------------------
   为什么不再用 SM-2 的 ease 倍率：
     SM-2 只记一个"倍率"，于是"昨天刚背的词"和"半年前背的词"用的是同一个乘数，
     它回答不了最关键的那个问题——**这个词现在还记得多少**。
     FSRS 把它拆成三个量，各自有各自的物理意义：

       S 稳定度（天）    记得有多牢。S=10 意味着"过了 10 天，还记得约九成"
       R 可提取性（0–1） 此刻还能想起来的概率。由"距上次过了多久 ÷ S"算出来
       D 难度（1–10）    这个词本身有多难。难词涨得慢、忘得快

     复习时刻的 R 是整套算法里最值钱的读数：
       刚到期就想起来（R≈0.9）——说明间隔刚好，小幅加长；
       拖了很久才想起（R≈0.4）——这次回忆更费力，想起来之后记得也更牢，稳定度大涨。
     这正是"必要难度"（desirable difficulty）的实验结论：
     **回忆越费力，记忆收益越大**，但费力到想不起来（R→0）就只剩遗忘了。

   与完整 FSRS 的差别（有意为之，不是偷懒）：
     完整版用 17 个按个人复习数据回归出来的权重，需要几千条作答才能拟合；
     这里用几个可解释的系数做出同样的形状（幂遗忘曲线 + 难度阻尼 + 费力增益），
     等数据攒够了再把系数换成回归值，接口不用动。
   ============================================================ */

export type Grade = "again" | "hard" | "good" | "easy";

/** FSRS 的评分：1 重来 / 2 困难 / 3 一般 / 4 简单 */
export const RATING: Record<Grade, number> = { again: 1, hard: 2, good: 3, easy: 4 };

export const GRADES: { id: Grade; label: string; hint: string; days: number; key: string }[] = [
  { id: "again", label: "Again", hint: "完全没想起来", days: 1, key: "1" },
  { id: "hard", label: "Hard", hint: "想起来了，但很吃力", days: 2, key: "2" },
  { id: "good", label: "Good", hint: "想起来了", days: 3, key: "3" },
  { id: "easy", label: "Easy", hint: "一眼就认识", days: 8, key: "4" },
];

/* ---------- 曲线常数（FSRS-4.5）---------- */
/** 遗忘曲线的形状：19/81 与 -0.5 是配套选的，正好让 R(S) = 0.9 */
const DECAY = -0.5;
const FACTOR = 19 / 81;

/** 期望保留率：默认让每个词在"还记得九成"的时候再见面 */
export const DESIRED_RETENTION = 0.9;
export const MAX_INTERVAL = 365;
const MIN_S = 0.4;

const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

/** 过了 elapsed 天，还记得多少（0–1） */
export function retrievability(elapsedDays: number, s: number): number {
  const ss = Math.max(MIN_S, s || 1);
  const t = Math.max(0, elapsedDays);
  return Math.pow(1 + (FACTOR * t) / ss, DECAY);
}

/** 想让保留率回到 target，还该等多久（天） */
export function daysForRetention(s: number, target = DESIRED_RETENTION): number {
  const ss = Math.max(MIN_S, s || 1);
  const t = (ss / FACTOR) * (Math.pow(target, 1 / DECAY) - 1);
  return clamp(Math.round(t), 1, MAX_INTERVAL);
}

/* ---------- 稳定度 ---------- */

/** 第一次见到一个词：按这一下的表现直接定初始稳定度（FSRS 的 initial stability） */
const INIT_S: Record<Grade, number> = { again: 0.5, hard: 1.2, good: 3.2, easy: 8 };
/** 之后每次复习的基础倍率 */
const GAIN: Record<Grade, number> = { again: 0, hard: 0.8, good: 1.6, easy: 2.4 };

export function stabilityAfter(
  prev: { s: number; d: number; reps: number },
  g: Grade,
  r: number,
): number {
  const s = Math.max(MIN_S, prev.s || 1);
  if (prev.reps === 0) return INIT_S[g];
  // 忘了：稳定度塌回去大半，但不会归零——毕竟曾经记住过
  if (g === "again") return Math.max(MIN_S, s * 0.4 + 0.3);

  const d = clamp(prev.d || 5, 1, 10);
  const hardWord = (11 - d) / 6; // 越难的词涨得越慢（d=5 时不快不慢）
  const effort = 1 + (1 - r) * 3; // 忘得越狠，想起来之后涨得越多
  return Math.min(MAX_INTERVAL * 1.5, s * GAIN[g] * effort * hardWord);
}

/* ---------- 难度 ---------- */

export function difficultyAfter(d0: number, g: Grade): number {
  const d = clamp(d0 || 5, 1, 10);
  const raw = d - 0.8 * (RATING[g] - 3);
  // 向中性难度（5）回归一点：否则一个词会一路被推到 10，再难也涨不动了
  return clamp(0.9 * raw + 0.1 * 5, 1, 10);
}

/* ---------- 间隔 ---------- */

/**
 * 下一次隔多久。Hard 用更高的保留率目标（0.95）→ 更早见面；
 * Easy 用更低的（0.85）→ 更晚。Again 一律明天（当天的重练由"小组轮转"负责）。
 */
export function intervalAfter(g: Grade, s: number): number {
  if (g === "again") return 1;
  if (g === "hard") return daysForRetention(s, 0.95);
  if (g === "easy") return daysForRetention(s, 0.85);
  return daysForRetention(s, DESIRED_RETENTION);
}

/* ============================================================
   会话内的"出组"门槛
   ------------------------------------------------------------
   一个词要在小组里连着答对三次，才从今天的小组里放走。
   为什么是"连着"：累计对会把"对对错对对"这种来回摇摆也算成会背，
   那正是"以为记住了其实没有"的模样。
   ============================================================ */
export const MASTERY = 3;

/** 定档封顶：这一轮中途错过，最终最高只给 good——不该算"一眼认出" */
export function capAt(g: Grade, max: Grade): Grade {
  const order: Grade[] = ["again", "hard", "good", "easy"];
  return order.indexOf(g) <= order.indexOf(max) ? g : max;
}

/* ============================================================
   自动定档：把"对错 + 用时"折算成 FSRS 的评分
   ------------------------------------------------------------
   为什么不让人自己点：手点四档时人几乎一律点 Good——"想起来了"和
   "盯着想了十秒"在手指上是同一档。反应时间是这两者的唯一可靠分水岭，
   而它恰恰是 R 的一个免费代理量：想得越久，说明 R 越低。
   ============================================================ */

/** 一次作答：对不对、用了多久 */
export type Answer = { ok: boolean; ms: number };

export function gradeOf(card: WordCardState | undefined, a: Answer): Grade {
  if (!a.ok) return "again";
  if (a.ms >= 9000) return "hard";
  if (a.ms < 3500) return "easy";
  const steady = !!card && card.lapses === 0 && card.reps >= 3;
  return steady && a.ms < 6000 ? "easy" : "good";
}

/** 界面上要能说出"为什么是这一档"——黑箱给分比不给分更让人不安 */
export function answerNote(a: Answer, g: Grade): string {
  // 答错不报耗时：人去泡了杯茶回来点错，写"用了 238.8 秒"只是噪音。
  if (!a.ok) return "选错了";
  // 超过一分钟也只说"一分钟以上"，别把走神时间当成绩展示
  const s = a.ms >= 60000 ? "1 分钟以上" : `${(a.ms / 1000).toFixed(1)} 秒`;
  if (g === "easy") return `一眼认出 · ${s}`;
  if (g === "good") return `想起来了 · ${s}`;
  return `想得有点吃力 · ${s}`;
}

/* ---------- 日期小工具 ---------- */

function dateStr(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function addDays(base: Date, days: number): string {
  const d = new Date(base.getTime());
  d.setDate(d.getDate() + days);
  return dateStr(d);
}

function daysBetween(from: string | undefined, to: Date): number | null {
  if (!from) return null;
  const a = new Date(`${from}T00:00:00`);
  if (Number.isNaN(a.getTime())) return null;
  const b = new Date(dateStr(to) + "T00:00:00");
  return Math.max(0, Math.round((b.getTime() - a.getTime()) / 86400000));
}

/* ---------- 卡片 ---------- */

/** 首次见到一个词 */
export function newCard(word: string, bookId: string, today: string): WordCardState {
  return {
    word,
    bookId,
    status: "new",
    ease: 2.5,
    intervalDays: 0,
    dueDate: today,
    reps: 0,
    lapses: 0,
    d: 5,
  };
}

/** 旧卡（只有 ease/intervalDays，没有 S/D）迁入 FSRS */
function migrate(card: WordCardState): { s: number; d: number } {
  if (card.s) return { s: card.s, d: clamp(card.d ?? 5, 1, 10) };
  // ease 越高越简单 → 难度越低
  const d = card.ease ? clamp(11 - 2 * card.ease, 1, 10) : 5;
  const s = card.intervalDays > 0 ? card.intervalDays : 1;
  return { s, d };
}

/** 评一档，返回新的卡片状态 */
export function grade(card: WordCardState, g: Grade, today = new Date()): WordCardState {
  const { s: s0, d: d0 } = migrate(card);
  // 距上次复习过了多久：没有记录就按"上次是准时复习的"算
  const elapsed = daysBetween(card.lastReviewAt, today) ?? (card.reps > 0 ? card.intervalDays : 0);
  const r = card.reps === 0 ? DESIRED_RETENTION : retrievability(elapsed, s0);

  const s1 = stabilityAfter({ s: s0, d: d0, reps: card.reps }, g, r);
  const d1 = difficultyAfter(d0, g);
  const interval = intervalAfter(g, s1);

  const status: WordCardState["status"] =
    g === "again" ? "learning" : interval >= 21 ? "done" : "review";

  return {
    ...card,
    s: s1,
    d: d1,
    lastR: r,
    lastReviewAt: dateStr(today),
    intervalDays: interval,
    dueDate: addDays(today, interval),
    reps: card.reps + 1,
    lapses: card.lapses + (g === "again" ? 1 : 0),
    status,
  };
}

/** 界面用：这个词的稳定度说人话是什么（"约 6 天后回落到九成"） */
export function stabilityNote(s: number | undefined): string {
  const v = Math.max(MIN_S, s ?? 0);
  if (v < 1) return "还很生";
  if (v < 3) return `记得 ${Math.round(v)} 天`;
  if (v < 30) return `记得约 ${Math.round(v)} 天`;
  if (v < 365) return `记得约 ${Math.round(v / 30)} 个月`;
  return "已经很牢了";
}
