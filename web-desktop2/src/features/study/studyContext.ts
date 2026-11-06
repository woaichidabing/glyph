/**
 * T06：给 Agent 的**只读**学习状态摘要。
 *
 * 桌面端的词书、SRS 卡片、错词都存在浏览器本地，Agent 看不见，于是它能批改变查词，
 * 却不能依据真实背词进度安排下一步。这份摘要就是把"今天该复习多少、哪些词老忘"
 * 用几行字告诉它。
 *
 * 三条硬边界：
 * 1. **只读**。这里只算事实交给 Agent 参考，改 SRS / 计划仍然只能由背词页或正式
 *    写入能力做 —— 摘要里也不带任何可回写的字段（不带 reps / lapses 明细）。
 * 2. **没数据就不给**。没选词书一律返回 null，让后端根本不注入这一段；
 *    Agent 拿不到摘要时按提示词要求说"不知道"，不许自己编进度。
 * 3. **只含核实过的口径**。到期数是事实（卡片 dueDate 到了就是到了），
 *    不掺每日额度这类界面选择；正确率、最近任务等口径未定的一律不进。
 *
 * 计算做成纯函数（不直接读 localStorage），这样才能离线反复打；
 * 读仓储的那一步单独放在 readStudySnapshot。
 */

import type { DailyQueue, ErrorWord, WordCardState } from "../../types";

/** 摘要的输入：一次快照，与存储来源解耦 */
export type StudySnapshot = {
  book: string | null;
  /** 当前词书的 id：到期数必须只算这一本，否则换书后会混进旧词书的卡 */
  bookId: string | null;
  mode: "plan" | "free";
  cards: Record<string, WordCardState>;
  errors: ErrorWord[];
  /** 今日队列；还没生成过就是 null */
  queue: DailyQueue | null;
  today: string; // YYYY-MM-DD
};

/** 交给后端的摘要；字段刻意少，够用就停 */
export type StudyContext = {
  book: string;
  mode: "plan" | "free";
  /** 今日到期的卡片数（事实，不受每日额度截断） */
  due: number;
  newTotal: number;
  newDone: number;
  reviewTotal: number;
  reviewDone: number;
  /** 反复遗忘的词（lapses ≥ 2），最多 5 个 */
  hardWords: string[];
};

/** 反复遗忘的门槛：错一次不算"反复"；类型注释也写了「≥2 才值得单独出卡」 */
const HARD_LAPSES = 2;
const HARD_MAX = 5;

export function summarizeStudy(s: StudySnapshot): StudyContext | null {
  // 没选词书 = 没有学习状态可言。返回 null 而不是一份全 0 的摘要，
  // 否则 Agent 会拿着"到期 0 个"当真，说出"你今天没有要复习的"这种没依据的话。
  if (!s.book) return null;

  // 到期数**只算当前词书**。摘要里"当前词书：X"和"今日到期：N 个"是连着说的，
  // 不过滤就会把换书之前那本的到期卡也算进来 —— Agent 于是会说
  // "你 CET4 有 4 个词到期"，其中两个其实是考研的，依据就错了。
  // 卡片缺 bookId 的一律不算：宁可少报，也不把来路不明的卡算成当前词书的功课。
  const due = Object.values(s.cards).filter(
    (c) => c.reps > 0 && c.dueDate <= s.today && c.bookId === s.bookId,
  ).length;

  const hardWords = s.errors
    .filter((e) => e.lapses >= HARD_LAPSES)
    .sort((a, b) => b.lapses - a.lapses)
    .slice(0, HARD_MAX)
    .map((e) => e.word);

  const q = s.queue;

  // 复习队列是跨词书建的，而切词书当天它不会重建（buildQueue 当天内复用）。
  // 于是队列里可能混着旧词书的词 —— 那个"已完成 x 个"是按整份队列记的，
  // 拆不出属于当前词书的部分。与其报一个跟"今日到期 N 个（当前词书）"
  // 对不上的进度，不如整个不报。
  const reviewWords = q?.reviewWords ?? [];
  const reviewMixed = reviewWords.some(
    (w) => s.cards[w]?.bookId && s.cards[w].bookId !== s.bookId,
  );

  return {
    book: s.book,
    mode: s.mode,
    due,
    newTotal: q?.newWords.length ?? 0,
    newDone: q?.doneNew ?? 0,
    reviewTotal: reviewMixed ? 0 : reviewWords.length,
    reviewDone: reviewMixed ? 0 : (q?.doneReview ?? 0),
    hardWords,
  };
}

/** 从本地仓储取一次快照（真正读 localStorage 的那一步） */
export async function readStudySnapshot(): Promise<StudySnapshot | null> {
  try {
    const study = await import("./studyRepo");
    const { todayStr } = await import("../../adapters/streakRepo");
    const s = study.getSettings();
    if (!s.bookId) return null;
    const { findBook } = await import("./wordbooks");
    const book = findBook(s.bookId);
    if (!book) return null;
    return {
      book: book.name ?? s.bookId,
      bookId: s.bookId,
      mode: s.mode,
      cards: study.getCards(),
      errors: study.getErrors(),
      queue: study.buildQueue(),
      today: todayStr(),
    };
  } catch {
    // 本地存储读不出来（隐私模式 / 数据坏）就当没有，不要让它炸掉发请求
    return null;
  }
}

/** 请求里要带的那一份；没数据就是 null，后端据此不注入 */
export async function buildStudyContext(): Promise<StudyContext | null> {
  const snap = await readStudySnapshot();
  return snap ? summarizeStudy(snap) : null;
}
