import type { DailyQueue, ErrorWord, WordCardState } from "../../types";
import { readJSON, writeJSON as rawWriteJSON } from "../../adapters/localStore";
import { bookWords, findBook } from "./wordbooks";
import { grade as applyGrade, newCard, MASTERY, type Grade } from "../../adapters/srs";
import { todayStr } from "../../adapters/streakRepo";
import { addMastered } from "../../adapters/masteredRepo";

/** 背单词的本地仓储。后端就绪后换成 /api/study/* ，调用方不动。 */

/**
 * 两种背法：
 *  - plan 定每日之量：每天推 dailyNew 个新词，背完今日方算圆满
 *  - free 随心背：不设额度，从上次停下的那个词接着走，爱背多少背多少
 *
 * 为什么是"模式"而不是"把每日之量填 0"：0 会被理解成"今天不背"，
 * 而随心背是"没有额度这回事"——两者的界面与完成判定完全不同。
 */
export type StudyMode = "plan" | "free";

/**
 * 队列没有"开 / 关"这一说：三条都是当天该做的功课，
 * 关掉一条只是让它从眼前消失，功课并没有少。所以设置里不留开关。
 */
export type Settings = {
  bookId: string | null;
  mode: StudyMode;
  /** 今日新学几个：从词书里没背过的词里取 */
  dailyNew: number;
  /** 今日复习几个：到期的词里取。和新学分开算——这是两件不同的功课 */
  dailyReview: number;
  /**
   * 一组同时放几个词在手上轮。
   * 为什么不是"一个词连着答三遍"：同一个词换个花样连着问三遍，
   * 第三次答对靠的是刚才的短时记忆，不是真记住了。
   * 一组五六个词轮着来，同一个词要绕完这一组才再见到它——
   * 中间隔着那几个词，就是最短的间隔重复。
   */
  groupSize: number;
  /** 翻到新词是否自动读一遍。默认关：先出声容易打扰，想听点喇叭，想自动读再来开 */
  autoSound: boolean;
  /** 每日提醒时间（写入日历用） */
  remindAt?: string;
};

const S_KEY = "agent.study.settings.v1";
const C_KEY = "agent.study.cards.v1";
const Q_KEY = "agent.study.queue.v1";
const E_KEY = "agent.study.errors.v1";
const F_KEY = "agent.study.free.v1";
const U_KEY = "agent.study.updatedAt.v1";

let changeListener: (() => void) | null = null;
let muted = false;

function writeJSON<T>(key: string, value: T): void {
  rawWriteJSON(key, value);
  if (!muted && key !== U_KEY) rawWriteJSON(U_KEY, Date.now());
  if (!muted) changeListener?.();
}

export function getUpdatedAt(): number {
  return readJSON<number>(U_KEY, 0);
}

/** store 注册一次；所有 studyRepo 写入后通知它去同步后端。 */
export function setStudyChangeListener(fn: (() => void) | null): void {
  changeListener = fn;
}

export type StudySnapshot = {
  version: 1;
  settings: Settings;
  queue: DailyQueue;
  cards: Record<string, WordCardState>;
  errors: ErrorWord[];
  free: FreeProgress | null;
  updatedAt: number;
};

export function snapshot(): StudySnapshot {
  return {
    version: 1,
    settings: getSettings(),
    queue: buildQueue(),
    cards: getCards(),
    errors: getErrors(),
    free: getFree(),
    updatedAt: getUpdatedAt() || Date.now(),
  };
}

export function restoreSnapshot(raw: unknown): boolean {
  if (!raw || typeof raw !== "object") return false;
  const s = raw as Partial<StudySnapshot>;
  if (!s.settings || !s.queue || !s.cards || !Array.isArray(s.errors)) return false;
  muted = true;
  try {
    writeJSON(S_KEY, s.settings);
    writeJSON(Q_KEY, s.queue);
    writeJSON(C_KEY, s.cards);
    writeJSON(E_KEY, s.errors);
    writeJSON(F_KEY, s.free ?? null);
    rawWriteJSON(U_KEY, Number(s.updatedAt) || 0);
  } finally {
    muted = false;
  }
  return true;
}

const DEFAULT: Settings = {
  bookId: null,
  mode: "plan",
  dailyNew: 30,
  dailyReview: 60,
  groupSize: 5,
  autoSound: false,
};

/* ---------------- 随心背的游标 ---------------- */

export type FreeProgress = {
  bookId: string;
  /** 下一个要背的是词表里的第几个（从 0 起） */
  cursor: number;
  /** 上次停下的那个词：界面上要能说出"上次背到 X" */
  word: string | null;
  updatedAt: number;
};

/** 换了词书，游标就作废——旧词表里的第 37 个跟新词表里的第 37 个不是一回事 */
export function getFree(): FreeProgress | null {
  const f = readJSON<FreeProgress | null>(F_KEY, null);
  if (!f) return null;
  if (f.bookId !== getSettings().bookId) return null;
  return f;
}

export function setFreeCursor(bookId: string, cursor: number, word: string | null): FreeProgress {
  const next: FreeProgress = { bookId, cursor, word, updatedAt: Date.now() };
  writeJSON(F_KEY, next);
  return next;
}

/**
 * 界面上要说的四件事：背到哪个、第几个、已经背会几个、还剩几个。
 *
 * 为什么"已背会"不能拿游标顶替：
 *   小组轮转之后，一组里的词不是按书里的顺序出师的。游标是"下一个要背的位置"，
 *   跟"背会了几个"是两回事——拿它当进度，背会了后头的词会显示成 0。
 *   所以这里按"有卡片、正经评过档"的词数算，跟背词页的进度环对得上。
 */
export function freeProgress(): {
  cursor: number;
  word: string | null;
  total: number;
  done: number;
  left: number;
} | null {
  const s = getSettings();
  if (!s.bookId || s.mode !== "free") return null;
  const book = findBook(s.bookId);
  if (!book) return null;
  const words = bookWords(book, 600);
  const total = words.length;
  const done = words.filter((w) => {
    const c = getCards()[w];
    return !!c && (c.reps > 0 || c.known === true);
  }).length;
  const f = getFree();
  const cursor = Math.min(f?.cursor ?? 0, total);
  return { cursor, word: f?.word ?? null, total, done, left: Math.max(0, total - done) };
}

/**
 * 背会一个词，把游标推到**第一个还没背会的词**。
 *
 * 为什么不能写 cursor = 下标 + 1：
 *   一组五六个词轮着考，先出师的往往不是排在第一个的那个。
 *   要照"下标 + 1"推，先出师的若是组里最后一个，游标一下跳过前头几个还没背的词，
 *   它们从此不再进队列——不是偷懒，是直接丢了。
 *   只越过"已经背会的"那段前缀，才两头都不亏：背会的不再回头，没背的一个不落。
 */
function advanceFree(word: string) {
  const s = getSettings();
  if (s.mode !== "free" || !s.bookId) return;
  const book = findBook(s.bookId);
  if (!book) return;
  const words = bookWords(book, 600);
  const cur = Math.min(getFree()?.cursor ?? 0, words.length);
  let next = cur;
  while (next < words.length && masteredToday(words[next])) next += 1;
  setFreeCursor(book.id, next, word);
}

export function getSettings(): Settings {
  return { ...DEFAULT, ...readJSON<Partial<Settings>>(S_KEY, {}) };
}

export function setSettings(patch: Partial<Settings>): Settings {
  const next = { ...getSettings(), ...patch };
  writeJSON(S_KEY, next);
  return next;
}

export function getCards(): Record<string, WordCardState> {
  return readJSON<Record<string, WordCardState>>(C_KEY, {});
}

export function getCard(word: string): WordCardState | undefined {
  return getCards()[word];
}

function putCard(card: WordCardState) {
  const all = getCards();
  all[card.word] = card;
  writeJSON(C_KEY, all);
}

export function getErrors(): ErrorWord[] {
  return readJSON<ErrorWord[]>(E_KEY, []);
}

export function addError(e: Omit<ErrorWord, "addedAt" | "goodStreak" | "lapses"> & Partial<ErrorWord>): ErrorWord[] {
  const all = getErrors().filter((x) => x.word !== e.word);
  const item: ErrorWord = {
    word: e.word,
    from: e.from,
    sentence: e.sentence,
    reason: e.reason,
    addedAt: Date.now(),
    goodStreak: e.goodStreak ?? 0,
    lapses: e.lapses ?? 0,
  };
  all.unshift(item);
  writeJSON(E_KEY, all);
  return all;
}

/** 撤销「移出错词本」用：按原 addedAt 插回原位 */
export function restoreError(item: ErrorWord): ErrorWord[] {
  const all = getErrors();
  if (all.some((x) => x.word === item.word)) return all;
  const next = [...all, item].sort((a, b) => b.addedAt - a.addedAt);
  writeJSON(E_KEY, next);
  return next;
}

/** 清空整个错词本（背词进度、卡片状态都不动）。 */
export function clearErrors(): ErrorWord[] {
  writeJSON(E_KEY, []);
  return [];
}

export function removeError(word: string): ErrorWord[] {
  const gone = getErrors().find((x) => x.word === word);
  const all = getErrors().filter((x) => x.word !== word);
  writeJSON(E_KEY, all);
  // 移出 = 这个词拿下了。留一笔账，成长页的"已拿下错词"靠它。
  // 没有这一笔，错词一移出就再无痕迹，等于把功劳簿撕了
  if (gone) addMastered(word, gone.from);
  return all;
}

/** 造一份空的今日队列 */
function emptyQueue(date: string): DailyQueue {
  return { date, newWords: [], reviewWords: [], wrongWords: [], doneNew: 0, doneReview: 0, doneWrong: 0 };
}

/** 每日首次进入背词页时生成；当天内复用同一份 */
export function buildQueue(force = false): DailyQueue {
  const today = todayStr();
  const stored = readJSON<DailyQueue | null>(Q_KEY, null);
  if (!force && stored && stored.date === today) {
    // 定每日之量的队列当天内是固定的，不动它
    if (getSettings().mode !== "free") return stored;
    // 随心背的队列是"从游标到书末"，游标一变它就是过期货。
    // 但光看游标不够：小组轮转下先出师的常常不是排在第一个的，游标可能一格不动，
    // 此时队列里那个已经背会的词若不滤掉，就会被重考一遍（进度还会多算一个）
    const cur0 = getFree()?.cursor ?? 0;
    if ((stored.freeFrom ?? 0) === cur0 && !stored.newWords.some((w) => masteredToday(w))) return stored;
  }

  const s = getSettings();
  const cards = getCards();
  const q = emptyQueue(today);

  if (s.bookId) {
    const book = findBook(s.bookId);
    if (book) {
      const words = bookWords(book, 600);
      // 随心背：不设额度，从游标一路到书末。今天背多少算多少
      if (s.mode === "free") {
        const cur = getFree()?.cursor ?? 0;
        // 已经背会的不再进队列：游标只越过背会的前缀，
        // 组里后出师的那些词还在游标之后站着，不滤掉就会再考一遍
        q.newWords = words.slice(cur).filter((w) => !masteredToday(w));
        q.freeFrom = cur;
      } else {
        q.newWords = words.filter((w) => !cards[w]).slice(0, s.dailyNew);
      }
    }
  }

  q.reviewWords = Object.values(cards)
    .filter((c) => c.reps > 0 && c.dueDate <= today)
    .sort((a, b) => (a.dueDate < b.dueDate ? -1 : 1))
    .map((c) => c.word)
    .slice(0, s.dailyReview);

  q.wrongWords = getErrors()
    .map((e) => e.word)
    .slice(0, 50);

  writeJSON(Q_KEY, q);
  return q;
}

export function saveQueue(q: DailyQueue) {
  writeJSON(Q_KEY, q);
}

/** 撤销评分用：把卡片状态写回去（传 null 表示抹掉这条记录） */
export function setCardState(card: WordCardState | null) {
  const all = getCards();
  if (!card) return;
  all[card.word] = card;
  writeJSON(C_KEY, all);
}

export function forgetCard(word: string) {
  const all = getCards();
  delete all[word];
  writeJSON(C_KEY, all);
}

export function decDone(kind: "new" | "review" | "wrong") {
  const q = buildQueue();
  if (kind === "new") q.doneNew = Math.max(0, q.doneNew - 1);
  else if (kind === "review") q.doneReview = Math.max(0, q.doneReview - 1);
  else q.doneWrong = Math.max(0, q.doneWrong - 1);
  saveQueue(q);
  return q;
}

export function bumpDone(kind: "new" | "review" | "wrong") {
  const q = buildQueue();
  if (kind === "new") q.doneNew += 1;
  else if (kind === "review") q.doneReview += 1;
  else q.doneWrong += 1;
  saveQueue(q);
  return q;
}

/**
 * 记一次评分：更新卡片 SRS 状态，同时维护错词本的连续答对次数。
 * 连续 3 次 Good / Easy 自动移出错词本。
 */
export function recordGrade(
  word: string,
  g: Grade,
  kind: "new" | "review" | "wrong",
  /**
   * 选择题式作答的结果：自动定档、"正确率"、连对次数都要靠它。
   * streak 由调用方算好传进来（答对 +1、答错归零、熟词直接记满）。
   */
  answer?: { ok: boolean; ms: number; streak?: number; known?: boolean },
) {
  const today = todayStr();
  const cards = getCards();
  const prev = cards[word] ?? newCard(word, getSettings().bookId ?? "unknown", today);
  const graded = applyGrade(prev, g);
  const card: WordCardState = answer
    ? {
        ...graded,
        seen: (prev.seen ?? 0) + (answer.known ? 0 : 1),
        right: (prev.right ?? 0) + (answer.ok ? 1 : 0),
        lastMs: answer.ms,
        streak: answer.streak ?? prev.streak,
        streakAt: answer.streak !== undefined ? today : prev.streakAt,
        known: answer.known ?? prev.known,
      }
    : graded;
  putCard(card);

  let removed = false;
  if (kind === "wrong") {
    const errors = getErrors();
    const idx = errors.findIndex((e) => e.word === word);
    if (idx >= 0) {
      const e = errors[idx];
      if (g === "again" || g === "hard") {
        errors[idx] = { ...e, goodStreak: 0, lapses: e.lapses + 1 };
      } else {
        const streak = e.goodStreak + 1;
        if (streak >= 3) {
          errors.splice(idx, 1);
          removed = true;
        } else {
          errors[idx] = { ...e, goodStreak: streak };
        }
      }
      writeJSON(E_KEY, errors);
    }
  }

  if (kind === "new") advanceFree(word);

  const q = bumpDone(kind);
  return { card, queue: q, removedFromErrors: removed };
}

/**
 * 只记连对次数，不动 SRS 档位。
 * 为什么分开：一个词要连对三次才算出师，中间那两次只是"还没过"，
 * 要是每次都正经评一档，同一个词一天内会被推三次，间隔直接飞到十五天去。
 */
export function setStreak(
  word: string,
  streak: number,
  known = false,
  /** 这一次作答也要记进"见过几次 / 对过几次"，否则正确率会少算 */
  tally?: { ok: boolean; ms: number },
) {
  const cards = getCards();
  const prev =
    cards[word] ?? newCard(word, getSettings().bookId ?? "unknown", todayStr());
  putCard({
    ...prev,
    streak,
    streakAt: todayStr(),
    known: known || prev.known,
    seen: tally ? (prev.seen ?? 0) + 1 : prev.seen,
    right: tally ? (prev.right ?? 0) + (tally.ok ? 1 : 0) : prev.right,
    lastMs: tally ? tally.ms : prev.lastMs,
  });
}

/** 今天已算出师（连对满三次，或标成熟词）——背词页的进度按它算 */
export function masteredToday(word: string): boolean {
  const c = getCards()[word];
  if (!c) return false;
  if (c.known) return true;
  return (c.streak ?? 0) >= MASTERY && c.streakAt === todayStr();
}

/** 今天的连对次数（隔天归零；熟词直接算满） */
export function streakToday(word: string): number {
  const c = getCards()[word];
  if (!c) return 0;
  if (c.known) return MASTERY;
  return c.streakAt === todayStr() ? Math.min(MASTERY, c.streak ?? 0) : 0;
}

/** 已背过的词数（用于词书进度环） */
export function learnedCount(bookId: string): number {
  return Object.values(getCards()).filter((c) => c.bookId === bookId && c.reps > 0).length;
}

/** 估算：按每天 dailyNew 个新词，约几天背完 + 完成日期 */
export function estimate(bookId: string, dailyNew: number): { days: number; finishDate: string } {
  const book = findBook(bookId);
  const total = book?.total ?? 0;
  const left = Math.max(0, total - learnedCount(bookId));
  const days = dailyNew > 0 ? Math.max(1, Math.ceil(left / dailyNew)) : 0;
  const d = new Date();
  d.setDate(d.getDate() + days);
  const p = (n: number) => String(n).padStart(2, "0");
  return { days, finishDate: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` };
}

/** 演示用：给错词本塞几条（后端接上后由作文批改写入） */
export function seedErrorsIfEmpty() {
  if (getErrors().length > 0) return;
  const demo: ErrorWord[] = [
    {
      word: "ambiguous",
      from: "2026-09-24 作文批改",
      sentence: "The contract was ambiguous about who would pay the fees.",
      reason: "把 ambiguous 当动词用，写成了 ambige",
      addedAt: Date.now(),
      goodStreak: 0,
      lapses: 2,
    },
    {
      word: "measure",
      from: "2026-09-24 作文批改",
      sentence: "The government should take measure to address the issue.",
      reason: "measure 作「措施」讲应为复数 measures",
      addedAt: Date.now() - 86400000,
      goodStreak: 1,
      lapses: 3,
    },
    {
      word: "comprise",
      from: "2026-09-22 作文批改",
      sentence: "The collection comprises 120 short stories.",
      reason: "误用被动 be comprised of",
      addedAt: Date.now() - 172800000,
      goodStreak: 0,
      lapses: 1,
    },
  ];
  writeJSON(E_KEY, demo);
}
