import type { Wordbook } from "../../types";
import { MINI_WORDS, MINI } from "../../adapters/miniDict";
import { readJSON, writeJSON } from "../../adapters/localStore";
import { fetchBooks, fetchWordPage, type BookEntry, type BookStat, type WordPage } from "../../adapters/wordbookApi";

/**
 * 词书：后端 ECDICT 按考试标签出真实词表（GET /api/wordbook/*）。
 *
 * 学习队列（studyRepo）是全同步的，为了不把异步传染到整条链路，
 * 词表走「异步预热 + 同步读缓存」：UI 进背词页前先 await syncBooks/ensureWords
 * 把书单和词表拉进 localStorage，studyRepo 照旧同步读 bookWords；
 * 后端不可用时降级到内置 mini 样例库（界面诚实标注「样例词库」）。
 */

/** 词书静态元数据（id 即 ECDICT 标签）；total 永远以后端真实统计为准，初始 0 */
export type BookMeta = Wordbook & {
  tag: string;
  /** 降级到 mini 样例库时用于匹配的中文标签（mini 库只有少量考试标签） */
  miniTags: string[];
};

const COVER = {
  shengxue: "#2E6B4F",
  daxue: "#5B7A8C",
  chuguo: "#7A4E9B",
} as const;

/** 后端词库真实支持的 8 本考试词书，顺序即展示顺序（由低阶到高阶） */
export const LOCAL_BOOKS: BookMeta[] = [
  { id: "zk", tag: "zk", name: "中考核心词", origin: "preset", group: "升学", total: 0, difficulty: 1, cover: COVER.shengxue, miniTags: ["中考"] },
  { id: "gk", tag: "gk", name: "高考核心词", origin: "preset", group: "升学", total: 0, difficulty: 2, cover: COVER.shengxue, miniTags: ["高考"] },
  { id: "cet4", tag: "cet4", name: "CET4 四级词", origin: "preset", group: "大学", total: 0, difficulty: 2, cover: COVER.daxue, miniTags: ["CET4"] },
  { id: "cet6", tag: "cet6", name: "CET6 六级词", origin: "preset", group: "大学", total: 0, difficulty: 3, cover: COVER.daxue, miniTags: ["CET6"] },
  { id: "ky", tag: "ky", name: "考研英语词", origin: "preset", group: "大学", total: 0, difficulty: 3, cover: COVER.daxue, miniTags: ["考研"] },
  { id: "ielts", tag: "ielts", name: "雅思核心词", origin: "preset", group: "出国", total: 0, difficulty: 3, cover: COVER.chuguo, miniTags: ["雅思"] },
  { id: "toefl", tag: "toefl", name: "托福核心词", origin: "preset", group: "出国", total: 0, difficulty: 4, cover: COVER.chuguo, miniTags: ["托福"] },
  { id: "gre", tag: "gre", name: "GRE 核心词", origin: "preset", group: "出国", total: 0, difficulty: 5, cover: COVER.chuguo, miniTags: ["GRE"] },
];

/* ---------------- 纯函数（缓存合并，可测） ---------------- */

/** 后端真实词量合并到本地元数据：统计里没有的书 total=0，绝不拿宣传数冒充 */
export function mergeStats(meta: BookMeta[], stats: BookStat[] | null): Wordbook[] {
  const totals = new Map((stats ?? []).map((s) => [s.tag, s.total]));
  return meta.map((m) => ({ ...m, total: totals.get(m.tag) ?? 0 }));
}

/** 一本词书已拉回的分页词表：pages 以 OFFSET 游标为键 */
export type StoredPage = {
  total: number;
  at: number;
  /** 下一页游标；null 表示已到书末 */
  next: number | null;
  pages: Record<number, string[]>;
  /** 轻量释义（词小写为键）；旧缓存没有这一字段，按缺省处理 */
  gloss?: Record<string, BookEntry>;
};

/** 合并一页：同游标重发去重覆盖，total 以后端最新值为准 */
export function mergePage(cached: StoredPage | null, page: WordPage): StoredPage {
  const gloss: Record<string, BookEntry> = { ...(cached?.gloss ?? {}) };
  for (const e of page.entries ?? []) {
    if (e.gloss) gloss[e.word.toLowerCase()] = e;
  }
  return {
    total: page.total,
    at: Date.now(),
    next: page.next_cursor,
    pages: { ...(cached?.pages ?? {}), [page.cursor]: [...page.words] },
    gloss,
  };
}

/**
 * 从分页缓存取从游标 0 开始的**连续**词，最多 limit 个。
 * 中间缺页就停在缺口前——跳过缺口会让一段词永远进不了队列（漏词）。
 */
export function takeWords(cached: StoredPage | null, limit: number): string[] {
  if (!cached) return [];
  const out: string[] = [];
  let cursor = 0;
  const keys = Object.keys(cached.pages).map(Number).sort((a, b) => a - b);
  for (const k of keys) {
    if (k !== cursor) break; // 连续前缀到此为止
    out.push(...cached.pages[k]);
    cursor += cached.pages[k].length;
    if (out.length >= limit) break;
  }
  return out.slice(0, limit);
}

/**
 * 按连续前缀取轻量词条（顺序与 takeWords 一致），供学习卡出选择题；
 * 没有 gloss 的旧缓存、或词缺释义时跳过——没释义做不了干扰项。
 */
export function takeGloss(cached: StoredPage | null, limit: number): BookEntry[] {
  if (!cached?.gloss) return [];
  const out: BookEntry[] = [];
  for (const w of takeWords(cached, limit)) {
    const e = cached.gloss[w.toLowerCase()];
    if (e?.gloss) out.push(e);
  }
  return out;
}

/** 后端不可用时的兜底：按 miniTags 从内置样例库取词 */
export function miniFallback(book: BookMeta, limit: number): string[] {
  return MINI_WORDS
    .filter((w) => MINI[w.toLowerCase()]?.tags?.some((t) => book.miniTags.includes(t)))
    .slice(0, limit);
}

/* ---------------- localStorage 缓存（侧效） ---------------- */

const BOOKS_KEY = "agent.wordbook.stats.v1";
const pageKey = (tag: string) => `agent.wordbook.words.v1:${tag}`;

export function saveBooksCache(stats: BookStat[]): void {
  writeJSON(BOOKS_KEY, { stats, at: Date.now() });
}

/** 同步读书单：合并本地元数据与缓存的真实词量（未同步时 total 全为 0） */
export function getBooks(): Wordbook[] {
  const cached = readJSON<{ stats: BookStat[] } | null>(BOOKS_KEY, null);
  return mergeStats(LOCAL_BOOKS, cached?.stats ?? null);
}

export function findBook(id: string | null | undefined): Wordbook | undefined {
  if (!id) return undefined;
  return getBooks().find((b) => b.id === id);
}

export function isSynced(book: Wordbook): boolean {
  return book.total > 0;
}

function readPage(tag: string): StoredPage | null {
  return readJSON<StoredPage | null>(pageKey(tag), null);
}

export function saveWordPage(tag: string, page: WordPage): StoredPage {
  const next = mergePage(readPage(tag), page);
  writeJSON(pageKey(tag), next);
  return next;
}

/**
 * 同步取一本书前 limit 个词：优先后端缓存，缓存为空时降级 mini 样例库。
 * studyRepo 的队列构建全同步，只认这个签名。
 */
export function bookWords(book: Wordbook, limit = 200): string[] {
  const cached = readPage(book.id);
  const words = takeWords(cached, limit);
  if (words.length > 0) return words;
  const meta = LOCAL_BOOKS.find((m) => m.id === book.id);
  return meta ? miniFallback(meta, limit) : [];
}

/** 这本书当前用的是真实后端词表，还是只剩 mini 兜底 */
export function bookWordsSource(book: Wordbook): "api" | "mini" {
  return readPage(book.id) ? "api" : "mini";
}

/**
 * 同步取一本书前 limit 个词的轻量词条（学习卡选择题干扰项池）；
 * 缓存里没有词条（旧后端/降级 mini）时返回空数组，调用方退回 mini 出题。
 */
export function bookGloss(book: Wordbook, limit = 600): BookEntry[] {
  return takeGloss(readPage(book.id), limit);
}

/* ---------------- 异步预热（UI 进背词页前调用） ---------------- */

/** 拉取并缓存真实书单（词量）；后端不可用时抛错，由调用方决定降级文案 */
export async function syncBooks(signal?: AbortSignal): Promise<Wordbook[]> {
  const stats = await fetchBooks(signal);
  saveBooksCache(stats);
  return getBooks();
}

const PAGE_SIZE = 600;

/**
 * 保证一本书前 limit 个词已在缓存（连续前缀），返回这些词。
 * 从当前连续前缀的末端逐页拉取，凑够或到书末即停。
 */
export async function ensureWords(book: Wordbook, limit = PAGE_SIZE, signal?: AbortSignal): Promise<string[]> {
  let cached = readPage(book.id);
  for (let guard = 0; guard < 50; guard += 1) {
    const have = takeWords(cached, Number.MAX_SAFE_INTEGER).length;
    if (have >= limit) break;
    const page = await fetchWordPage(book.id, have, PAGE_SIZE, signal);
    cached = saveWordPage(book.id, page);
    if (page.next_cursor === null) break; // 书末
  }
  return bookWords(book, limit);
}

/** 试学预览：前 10 词（未预热时可能是 mini 兜底） */
export function previewWords(book: Wordbook): string[] {
  return bookWords(book, 10);
}
