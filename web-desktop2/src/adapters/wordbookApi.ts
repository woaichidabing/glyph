/**
 * 词书后端接口：GET /api/wordbook/books（书单+真实词量）、
 * GET /api/wordbook/words（按标签分页取词）。
 * 词库未就绪时后端回 503，这里原样抛错，由 wordbooks 的预热层降级 mini。
 */

export type BookStat = { tag: string; label: string; total: number };

/** 词书页内的轻量词条：只有学习卡出选择题需要的词性与第一释义（完整释义走 /api/dict/lookup） */
export type BookEntry = {
  word: string;
  phonetic: string;
  pos: string | null;
  gloss: string;
};

export type WordPage = {
  tag: string;
  total: number;
  cursor: number;
  next_cursor: number | null;
  words: string[];
  /** 与 words 同序同长；旧后端可能不下发，按缺省空表处理 */
  entries?: BookEntry[];
};

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error("词库暂时不可用，请稍后重试。");
  return (await res.json()) as T;
}

export async function fetchBooks(signal?: AbortSignal): Promise<BookStat[]> {
  const data = await getJson<{ books: BookStat[] }>("/api/wordbook/books", signal);
  return data.books;
}

export async function fetchWordPage(
  tag: string,
  cursor = 0,
  limit = 600,
  signal?: AbortSignal,
): Promise<WordPage> {
  const q = new URLSearchParams({ tag, cursor: String(cursor), limit: String(limit) });
  return getJson<WordPage>(`/api/wordbook/words?${q.toString()}`, signal);
}
