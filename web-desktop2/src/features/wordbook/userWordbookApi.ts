/**
 * 「我的生词本 / 查词历史」后端接口（/api/user/wordbook*）。
 *
 * 契约（与后端同一份）：
 *   GET    /api/user/wordbook                        -> {items, history}
 *   POST   /api/user/wordbook {entry}                -> {items, history, item}
 *   DELETE /api/user/wordbook/{word}                 -> {items, history, removed}
 *   DELETE /api/user/wordbook                        -> {items: [], history}
 *   POST   /api/user/wordbook/restore {item}         -> {items, history, item}
 *   POST   /api/user/wordbook/history {word}         -> {items, history, item}
 *   DELETE /api/user/wordbook/history                -> {items, history: []}
 *   POST   /api/user/wordbook/import {items, history} -> {items, history, importedItems, importedHistory}
 *
 * 只做三件事：拼相对 /api 路径（vite 代理）、发 JSON、把契约原样交回。
 * 非 2xx 一律抛 Error(detail 或状态码)：store 靠它决定回滚，绝不把失败伪装成成功。
 */

import type { DictEntry, HistoryItem, WordbookItem } from "../../types";

/** 后端快照：生词本 + 历史（所有写接口都顺带回一份，前端直接覆盖） */
export type Snapshot = { items: WordbookItem[]; history: HistoryItem[] };

export type WordResult = Snapshot & { item: WordbookItem };
/** removed：被移出的条目；词本来就不在时是 null（后端不报 404） */
export type RemoveResult = Snapshot & { removed: WordbookItem | null };
export type HistoryResult = Snapshot & { item: HistoryItem };
export type ImportResult = Snapshot & {
  importedItems: number;
  importedHistory: number;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = init ? await fetch(path, init) : await fetch(path);
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 的错误页：下面按状态码抛 */
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new Error(
      typeof detail === "string" && detail ? detail : `请求失败（HTTP ${res.status}）`,
    );
  }
  return data as T;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

const BASE = "/api/user/wordbook";

export function getState(): Promise<Snapshot> {
  return request<Snapshot>(BASE);
}

export function addWord(entry: DictEntry): Promise<WordResult> {
  return request<WordResult>(BASE, jsonInit("POST", { entry }));
}

export function removeWord(word: string): Promise<RemoveResult> {
  return request<RemoveResult>(`${BASE}/${encodeURIComponent(word.trim())}`, { method: "DELETE" });
}

export function restoreWord(item: WordbookItem): Promise<WordResult> {
  return request<WordResult>(`${BASE}/restore`, jsonInit("POST", { item }));
}

export function clearWords(): Promise<Snapshot> {
  return request<Snapshot>(BASE, { method: "DELETE" });
}

export function recordLookup(word: string): Promise<HistoryResult> {
  return request<HistoryResult>(`${BASE}/history`, jsonInit("POST", { word }));
}

export function clearHistory(): Promise<Snapshot> {
  return request<Snapshot>(`${BASE}/history`, { method: "DELETE" });
}

export function importState(
  items: WordbookItem[],
  history: HistoryItem[],
): Promise<ImportResult> {
  return request<ImportResult>(`${BASE}/import`, jsonInit("POST", { items, history }));
}
