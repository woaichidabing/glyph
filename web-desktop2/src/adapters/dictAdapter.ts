import type { DictEntry } from "../types";
import { HOT_WORDS, MINI, MINI_WORDS } from "./miniDict";

/**
 * 词典适配器：三级降级
 *   ① 内存 LRU（200 条）→ ② `/api/dict/lookup`（后端 ECDICT）→ ③ 内置 mini 词库
 * 组件永不直接 fetch，后端就绪只换这一层。
 */

const CACHE_MAX = 200;
const cache = new Map<string, DictEntry>();

function touch(key: string, entry: DictEntry) {
  cache.delete(key);
  cache.set(key, entry);
  while (cache.size > CACHE_MAX) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

/** 查词未收录 */
export class NotFoundError extends Error {
  constructor(word: string) {
    super(`未收录：${word}`);
    this.name = "NotFoundError";
  }
}

/** 后端词库暂时不可用，且 mini 词库也没有 */
export class DictUnavailableError extends Error {
  constructor() {
    super("词库暂不可用");
    this.name = "DictUnavailableError";
  }
}

function normalize(raw: unknown, word: string): DictEntry | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const senses = Array.isArray(r.senses) ? (r.senses as DictEntry["senses"]) : [];
  if (senses.length === 0) return null;
  const ph = (r.phonetic ?? {}) as Record<string, unknown>;
  return {
    word: String(r.word ?? word),
    phonetic: {
      uk: typeof ph.uk === "string" ? ph.uk : "",
      us: typeof ph.us === "string" ? ph.us : "",
    },
    audio: (r.audio ?? undefined) as DictEntry["audio"],
    senses,
    forms: Array.isArray(r.forms) ? (r.forms as string[]) : undefined,
    synonyms: Array.isArray(r.synonyms) ? (r.synonyms as string[]) : undefined,
    antonyms: Array.isArray(r.antonyms) ? (r.antonyms as string[]) : undefined,
    tags: Array.isArray(r.tags) ? (r.tags as string[]) : undefined,
    definitions: Array.isArray(r.definitions)
      ? (r.definitions as unknown[]).filter((x): x is string => typeof x === "string")
      : undefined,
    queried_form: typeof r.queried_form === "string" ? r.queried_form : undefined,
    base_form: typeof r.base_form === "string" ? r.base_form : undefined,
    source: "api",
  };
}

export async function lookup(word: string, signal?: AbortSignal): Promise<DictEntry> {
  const raw = word.trim();
  const key = raw.toLowerCase();
  if (!key) throw new NotFoundError(raw);

  const hit = cache.get(key);
  if (hit) {
    touch(key, hit);
    return hit;
  }

  // ② 后端
  try {
    const res = await fetch("/api/dict/lookup", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ word: key }),
      signal,
    });
    if (res.ok) {
      const entry = normalize(await res.json(), raw);
      if (entry) {
        touch(key, entry);
        return entry;
      }
    }
  } catch {
    /* 后端未起或超时，静默降级到 mini */
  }

  // ③ mini 词库
  const mini = MINI[key];
  if (mini) {
    const entry: DictEntry = { ...mini, word: mini.word, source: "mini" };
    touch(key, entry);
    return entry;
  }

  throw new NotFoundError(raw);
}

/** 输入联想：mini 词库前缀匹配，最多 n 条；后端就绪后可换成 /api/dict/suggest */
export function suggest(prefix: string, n = 8): string[] {
  const p = prefix.trim().toLowerCase();
  if (!p) return [];
  const out: string[] = [];
  for (const w of MINI_WORDS) {
    if (w.toLowerCase().startsWith(p)) {
      out.push(w);
      if (out.length >= n) return out;
    }
  }
  for (const w of MINI_WORDS) {
    if (!out.includes(w) && w.toLowerCase().includes(p)) {
      out.push(w);
      if (out.length >= n) return out;
    }
  }
  return out;
}

export function hotWords(): string[] {
  return HOT_WORDS;
}

/** 兜底发音端点：有道公开 dictvoice（已实测 200 / audio/mpeg / 免 key） */
export function audioUrl(word: string, accent: "uk" | "us"): string {
  return `https://dict.youdao.com/dictvoice?audio=${encodeURIComponent(word)}&type=${accent === "uk" ? 1 : 2}`;
}
