import type { HistoryItem, WordbookItem } from "../../types";
import { readJSON } from "../../adapters/localStore";

/**
 * 生词本 / 查词历史的**旧版 localStorage 账本**（只读）。
 *
 * 真相源已切到后端 `/api/user/wordbook`（见 userWordbookApi.ts 与
 * wordbookStore.ts 的一次性迁移）。这里只有两件事：
 *   1. 保留读旧键的能力，供迁移那一次使用；
 *   2. 纯函数导出（调用方从 store 取 items 传进来）。
 * **不再写入 localStorage**——两处同时维护第二份真相必然漂移。
 */

export const LEGACY_BOOK_KEY = "agent.wordbook.v1";
export const LEGACY_HISTORY_KEY = "agent.history.v1";

/** 旧生词本（仅迁移用；坏数据由 readJSON 兜成空） */
export function listBook(): WordbookItem[] {
  return readJSON<WordbookItem[]>(LEGACY_BOOK_KEY, []);
}

/** 旧查词历史（仅迁移用） */
export function listHistory(): HistoryItem[] {
  return readJSON<HistoryItem[]>(LEGACY_HISTORY_KEY, []);
}

/** 导出为纯文本，供"导出到别处"用；条目由调用方从 store 取（不再读 localStorage） */
export function exportBookAsText(items: WordbookItem[]): string {
  return items.map((i) => `${i.word}\t${i.phonetic?.uk ?? ""}\t${i.gloss ?? ""}`).join("\n");
}
