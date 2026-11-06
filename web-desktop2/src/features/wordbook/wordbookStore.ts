import { create } from "zustand";
import type { DictEntry, HistoryItem, WordbookItem } from "../../types";
import * as api from "./userWordbookApi";
import { LEGACY_BOOK_KEY, LEGACY_HISTORY_KEY } from "./wordbookRepo";
import { readJSON } from "../../adapters/localStore";

/**
 * 生词本 / 查词历史：真相源在后端 `/api/user/wordbook`。
 *
 * - localStorage 只在 hydrate 时**读一次**做迁移，此后不再写（旧债一次还清）。
 * - 写动作一律先乐观更新、再等后端；失败回滚并 set error。
 * - 所有动作返回的 Promise **永不 reject**：旧调用点（DictView / StudyView）不 await
 *   也不会产生未处理拒绝；乐观更新已经先落界面。
 */

/** 迁移标记：置 "1" 表示旧 localStorage 账本已搬完，以后不再 import */
export const BACKEND_FLAG_KEY = "agent.wordbook.backend.v1";

/** 历史最多留 50 条（和后端同口径；最终以后端快照为准） */
const HISTORY_MAX = 50;

const norm = (word: string) => word.trim().toLowerCase();
const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function hasMigrated(): boolean {
  try {
    return localStorage.getItem(BACKEND_FLAG_KEY) === "1";
  } catch {
    return false;
  }
}

function markMigrated(): void {
  try {
    localStorage.setItem(BACKEND_FLAG_KEY, "1");
  } catch {
    /* 标记写不进去最多下次再 import 一遍；后端 import 幂等，不会重复计 */
  }
}

/** 旧键读出来必须真是数组才认，坏数据一律当空 */
function legacyArray<T>(key: string): T[] {
  const got = readJSON<unknown>(key, []);
  return Array.isArray(got) ? (got as T[]) : [];
}

export type BookState = {
  items: WordbookItem[];
  history: HistoryItem[];
  /** 首次快照（迁移 + GET）是否已落定；失败也算落定（error 里有原因） */
  ready: boolean;
  loading: boolean;
  error: string | null;

  hydrate: () => Promise<void>;
  refresh: () => Promise<void>;
  add: (entry: DictEntry) => Promise<void>;
  remove: (word: string) => Promise<WordbookItem | null>;
  restore: (item: WordbookItem) => Promise<void>;
  clear: () => Promise<void>;
  has: (word: string) => boolean;
  remember: (word: string) => Promise<void>;
  clearHistory: () => Promise<void>;
};

/** 并发去重：hydrate 被多处同时调时只跑一轮 */
let hydrating: Promise<void> | null = null;

export const useWordbook = create<BookState>((set, get) => {
  /** 只在响应里真有数组时才覆盖，坏响应不会把已有数据抹成空 */
  const applySnapshot = (snap: Partial<api.Snapshot>) => {
    set({
      ...(Array.isArray(snap.items) ? { items: snap.items } : {}),
      ...(Array.isArray(snap.history) ? { history: snap.history.slice(0, HISTORY_MAX) } : {}),
      error: null,
    });
  };

  /** 一次性迁移：只有旧数据非空且没打过标记才 import */
  const migrateLegacy = async () => {
    if (hasMigrated()) return;
    const items = legacyArray<WordbookItem>(LEGACY_BOOK_KEY);
    const history = legacyArray<HistoryItem>(LEGACY_HISTORY_KEY);
    if (items.length === 0 && history.length === 0) return;
    await api.importState(items, history);
    markMigrated();
  };

  const runHydrate = async () => {
    set({ loading: true, error: null });
    try {
      await migrateLegacy();
      applySnapshot(await api.getState());
      set({ ready: true, loading: false });
    } catch (e) {
      // 失败也要落定：页面显示空账 + 错误提示，而不是永远转圈
      set({ ready: true, loading: false, error: errorText(e) });
    }
  };

  return {
    items: [],
    history: [],
    ready: false,
    loading: false,
    error: null,

    hydrate: () => {
      if (hydrating) return hydrating;
      if (get().ready) return Promise.resolve();
      hydrating = runHydrate().finally(() => {
        hydrating = null;
      });
      return hydrating;
    },

    refresh: () => {
      // 还没 hydrate 过就补一次（DictView 切页时只调 refresh，不能让它空着）
      if (!get().ready) return get().hydrate();
      return (async () => {
        set({ loading: true, error: null });
        try {
          applySnapshot(await api.getState());
        } catch (e) {
          set({ error: errorText(e) });
        } finally {
          set({ loading: false });
        }
      })();
    },

    add: async (entry) => {
      const before = get().items;
      const key = norm(entry.word);
      const optimistic: WordbookItem = {
        word: entry.word,
        addedAt: Date.now(),
        phonetic: entry.phonetic,
        ...(entry.senses?.[0]?.translation ? { gloss: entry.senses[0].translation } : {}),
      };
      set({ items: [optimistic, ...before.filter((i) => norm(i.word) !== key)], error: null });
      try {
        applySnapshot(await api.addWord(entry));
      } catch (e) {
        set({ items: before, error: errorText(e) });
      }
    },

    remove: async (word) => {
      const before = get().items;
      const key = norm(word);
      const found = before.find((i) => norm(i.word) === key) ?? null;
      if (!found) return null;
      set({ items: before.filter((i) => norm(i.word) !== key), error: null });
      try {
        applySnapshot(await api.removeWord(word));
        return found;
      } catch (e) {
        set({ items: before, error: errorText(e) });
        return null;
      }
    },

    restore: async (item) => {
      const before = get().items;
      const key = norm(item.word);
      // 撤销「移出」：按原 addedAt 插回原位，不排到最前面
      const merged = before.some((i) => norm(i.word) === key)
        ? before
        : [...before, item].sort((a, b) => b.addedAt - a.addedAt);
      set({ items: merged, error: null });
      try {
        applySnapshot(await api.restoreWord(item));
      } catch (e) {
        set({ items: before, error: errorText(e) });
      }
    },

    clear: async () => {
      const before = get().items;
      set({ items: [], error: null });
      try {
        applySnapshot(await api.clearWords());
      } catch (e) {
        set({ items: before, error: errorText(e) });
      }
    },

    has: (word) => {
      const key = norm(word);
      return get().items.some((i) => norm(i.word) === key);
    },

    remember: async (word) => {
      const trimmed = word.trim();
      const before = get().history;
      const key = norm(trimmed);
      const prev = before.find((h) => norm(h.word) === key);
      // 同词 count+1、at=now、移到最前、裁 50
      const next: HistoryItem[] = [
        { word: trimmed, at: Date.now(), count: prev ? (prev.count ?? 1) + 1 : 1 },
        ...before.filter((h) => norm(h.word) !== key),
      ].slice(0, HISTORY_MAX);
      set({ history: next, error: null });
      try {
        applySnapshot(await api.recordLookup(trimmed));
      } catch (e) {
        set({ history: before, error: errorText(e) });
      }
    },

    clearHistory: async () => {
      const before = get().history;
      set({ history: [], error: null });
      try {
        applySnapshot(await api.clearHistory());
      } catch (e) {
        set({ history: before, error: errorText(e) });
      }
    },
  };
});
