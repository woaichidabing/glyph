/**
 * 生词本 store 的行为契约（TDD）。
 *
 * 重点钉四件事：
 *   1. hydrate 并发去重；旧 localStorage 只迁移一次（标记 agent.wordbook.backend.v1）
 *   2. 写动作先乐观更新，失败回滚并记 error
 *   3. remember 对同词 count+1、at=now、移到最前、最多 50 条
 *   4. 后端不可达时 ready=true + error，页面不崩
 *
 * node 测试环境没有 localStorage / fetch，全部用 vi.stubGlobal 造假。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DictEntry, HistoryItem, WordbookItem } from "../../types";
import { useWordbook } from "./wordbookStore";

const LEGACY_BOOK = "agent.wordbook.v1";
const LEGACY_HISTORY = "agent.history.v1";
const BACKEND_FLAG = "agent.wordbook.backend.v1";

const ITEM: WordbookItem = {
  word: "commit",
  addedAt: 1_700_000_000_000,
  phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
  gloss: "承诺，犯（罪）",
};

const ENTRY: DictEntry = {
  word: "commit",
  phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
  senses: [{ pos: "v.", translation: "承诺，犯（罪）", examples: [] }],
  source: "api",
};

function memoryStorage(seed: Record<string, string> = {}): Storage {
  const map = new Map(Object.entries(seed));
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => (map.has(key) ? map.get(key)! : null),
    key: (index: number) => Array.from(map.keys())[index] ?? null,
    removeItem: (key: string) => {
      map.delete(key);
    },
    setItem: (key: string, value: string) => {
      map.set(key, String(value));
    },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** 按 URL 分流的 fetch 假实现：只认 GET 快照与 import 两个端点 */
function routedFetch(routes: {
  imported?: unknown;
  snapshot?: unknown;
  onGet?: () => Promise<Response>;
}) {
  return vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/user/wordbook/import") {
      return jsonResponse(
        routes.imported ?? { items: [], history: [], importedItems: 0, importedHistory: 0 },
      );
    }
    if (url === "/api/user/wordbook" && (!init || init.method === undefined)) {
      if (routes.onGet) return routes.onGet();
      return jsonResponse(routes.snapshot ?? { items: [], history: [] });
    }
    throw new Error("unexpected fetch: " + url);
  });
}

function emptyState() {
  return {
    items: [] as WordbookItem[],
    history: [] as HistoryItem[],
    ready: false,
    loading: false,
    error: null as string | null,
  };
}

beforeEach(() => {
  vi.unstubAllGlobals();
  useWordbook.setState(emptyState());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("hydrate · 一次性迁移", () => {
  it("旧数据非空且无标记：先 import 再 GET，标记写回；并发调用只跑一轮", async () => {
    const storage = memoryStorage({
      [LEGACY_BOOK]: JSON.stringify([ITEM]),
      [LEGACY_HISTORY]: JSON.stringify([{ word: "commit", at: 1 }]),
    });
    vi.stubGlobal("localStorage", storage);
    const fetchMock = routedFetch({
      imported: { items: [ITEM], history: [{ word: "commit", at: 1 }], importedItems: 1, importedHistory: 1 },
      snapshot: { items: [ITEM], history: [{ word: "commit", at: 1 }] },
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.all([useWordbook.getState().hydrate(), useWordbook.getState().hydrate()]);

    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls.filter((u) => u === "/api/user/wordbook/import")).toHaveLength(1);
    expect(urls.filter((u) => u === "/api/user/wordbook")).toHaveLength(1);
    expect(storage.getItem(BACKEND_FLAG)).toBe("1");

    const state = useWordbook.getState();
    expect(state.ready).toBe(true);
    expect(state.loading).toBe(false);
    expect(state.items).toEqual([ITEM]);
    expect(state.history).toEqual([{ word: "commit", at: 1 }]);

    // 已 ready 再 hydrate 不再打接口（迁移只跑一次）
    await useWordbook.getState().hydrate();
    expect(fetchMock.mock.calls).toHaveLength(2);
  });

  it("已有迁移标记：不再 import，只拉快照", async () => {
    const storage = memoryStorage({
      [BACKEND_FLAG]: "1",
      [LEGACY_BOOK]: JSON.stringify([ITEM]),
      [LEGACY_HISTORY]: JSON.stringify([{ word: "commit", at: 1 }]),
    });
    vi.stubGlobal("localStorage", storage);
    const fetchMock = routedFetch({ snapshot: { items: [], history: [] } });
    vi.stubGlobal("fetch", fetchMock);

    await useWordbook.getState().hydrate();

    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(["/api/user/wordbook"]);
    expect(storage.getItem(BACKEND_FLAG)).toBe("1");
  });

  it("旧键坏数据当空：不触发 import", async () => {
    const storage = memoryStorage({
      [LEGACY_BOOK]: "{不是 json",
      [LEGACY_HISTORY]: JSON.stringify({ not: "an array" }),
    });
    vi.stubGlobal("localStorage", storage);
    const fetchMock = routedFetch({ snapshot: { items: [], history: [] } });
    vi.stubGlobal("fetch", fetchMock);

    await useWordbook.getState().hydrate();

    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(["/api/user/wordbook"]);
  });

  it("后端不可达：ready=true + error，不抛不崩", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, _init?: RequestInit) =>
        jsonResponse({ detail: "后端没启动" }, 500),
      ),
    );

    await expect(useWordbook.getState().hydrate()).resolves.toBeUndefined();

    const state = useWordbook.getState();
    expect(state.ready).toBe(true);
    expect(state.loading).toBe(false);
    expect(state.error).toBe("后端没启动");
    expect(state.items).toEqual([]);
  });
});

describe("add · 乐观更新与回滚", () => {
  it("先落界面，后端失败回滚并记 error", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    useWordbook.setState({ ready: true });
    let release!: (r: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", vi.fn((_url: string, _init?: RequestInit) => pending));

    const saving = useWordbook.getState().add(ENTRY);

    const optimistic = useWordbook.getState().items;
    expect(optimistic).toHaveLength(1);
    expect(optimistic[0]).toMatchObject({
      word: "commit",
      phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
      gloss: "承诺，犯（罪）",
    });
    expect(optimistic[0].addedAt).toBeGreaterThan(0);

    release(jsonResponse({ detail: "存不进去" }, 500));
    await saving;

    expect(useWordbook.getState().items).toEqual([]);
    expect(useWordbook.getState().error).toBe("存不进去");
  });

  it("成功后采用后端快照", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    useWordbook.setState({ ready: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, _init?: RequestInit) =>
        jsonResponse({ items: [{ ...ITEM, addedAt: 99 }], history: [], item: ITEM }),
      ),
    );

    await useWordbook.getState().add(ENTRY);

    expect(useWordbook.getState().items).toEqual([{ ...ITEM, addedAt: 99 }]);
    expect(useWordbook.getState().error).toBeNull();
  });
});

describe("remember · 查词历史", () => {
  it("同词 count+1、at=now、移到最前", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    useWordbook.setState({
      ready: true,
      history: [
        { word: "other", at: 10, count: 3 },
        { word: "commit", at: 20, count: 1 },
      ],
    });
    const releases: ((r: Response) => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, _init?: RequestInit) => new Promise<Response>((resolve) => releases.push(resolve))),
    );

    const before = Date.now();
    const first = useWordbook.getState().remember("commit");

    const optimistic = useWordbook.getState().history;
    expect(optimistic[0].word).toBe("commit");
    expect(optimistic[0].count).toBe(2);
    expect(optimistic[0].at).toBeGreaterThanOrEqual(before);
    expect(optimistic[1].word).toBe("other");
    releases.shift()!(jsonResponse({
      items: [],
      history: [{ word: "commit", at: 21, count: 2 }, { word: "other", at: 10, count: 3 }],
      item: { word: "commit", at: 21, count: 2 },
    }));
    await first;

    // 第二次：服务端已回 count=2，再查一次应到 3
    const second = useWordbook.getState().remember("commit");
    expect(useWordbook.getState().history[0].count).toBe(3);
    releases.shift()!(jsonResponse({
      items: [],
      history: [{ word: "commit", at: 22, count: 3 }],
      item: { word: "commit", at: 22, count: 3 },
    }));
    await second;

    expect(useWordbook.getState().history[0]).toMatchObject({ word: "commit", count: 3 });
  });

  it("最多留 50 条（乐观更新这一瞬间就裁掉）", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    useWordbook.setState({
      ready: true,
      history: Array.from({ length: 50 }, (_, i) => ({ word: "w" + i, at: 100 - i, count: 1 })),
    });
    let release!: (r: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", vi.fn((_url: string, _init?: RequestInit) => pending));

    const saving = useWordbook.getState().remember("new");

    const capped = useWordbook.getState().history;
    expect(capped).toHaveLength(50);
    expect(capped[0].word).toBe("new");

    release(jsonResponse({
      items: [],
      history: capped,
      item: { word: "new", at: 999, count: 1 },
    }));
    await saving;
    expect(useWordbook.getState().history).toHaveLength(50);
  });

  it("后端失败回滚历史并记 error", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    useWordbook.setState({ ready: true, history: [{ word: "old", at: 1, count: 1 }] });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ detail: "历史写不进" }, 500)),
    );

    await useWordbook.getState().remember("new");

    expect(useWordbook.getState().history).toEqual([{ word: "old", at: 1, count: 1 }]);
    expect(useWordbook.getState().error).toBe("历史写不进");
  });
});

describe("remove / restore · 撤销链路", () => {
  it("remove 返回被删条目并乐观移出；restore 按 addedAt 插回原位", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    const older: WordbookItem = { word: "alpha", addedAt: 5 };
    useWordbook.setState({ ready: true, items: [{ ...ITEM, addedAt: 10 }, older] });

    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "DELETE" && url === "/api/user/wordbook/commit") {
        return jsonResponse({ items: [older], history: [], removed: ITEM });
      }
      if (url === "/api/user/wordbook/restore") {
        return jsonResponse({ items: [{ ...ITEM, addedAt: 10 }, older], history: [], item: ITEM });
      }
      throw new Error("unexpected fetch: " + url);
    });
    vi.stubGlobal("fetch", fetchMock);

    const removed = await useWordbook.getState().remove("commit");
    expect(removed).toMatchObject({ word: "commit", addedAt: 10 });
    expect(useWordbook.getState().items).toEqual([older]);

    await useWordbook.getState().restore(removed!);
    expect(useWordbook.getState().items.map((i) => i.word)).toEqual(["commit", "alpha"]);
  });

  it("恢复后按 addedAt 倒序，不会被排到最前", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    const older: WordbookItem = { word: "alpha", addedAt: 5 };
    useWordbook.setState({ ready: true, items: [older] });
    let release!: (r: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal("fetch", vi.fn((_url: string, _init?: RequestInit) => pending));

    const restoring = useWordbook.getState().restore({ ...ITEM, addedAt: 10 });

    // 乐观更新这一瞬间就该按 addedAt 倒序插回，而不是排到最前
    expect(useWordbook.getState().items.map((i) => i.word)).toEqual(["commit", "alpha"]);

    release(jsonResponse({ items: [{ ...ITEM, addedAt: 10 }, older], history: [], item: ITEM }));
    await restoring;
    expect(useWordbook.getState().items.map((i) => i.word)).toEqual(["commit", "alpha"]);
  });
});

describe("clear / clearHistory · 失败回滚", () => {
  it("清空生词本失败时把词还回来", async () => {
    vi.stubGlobal("localStorage", memoryStorage());
    useWordbook.setState({ ready: true, items: [ITEM] });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ detail: "清空失败" }, 500)),
    );

    await useWordbook.getState().clear();

    expect(useWordbook.getState().items).toEqual([ITEM]);
    expect(useWordbook.getState().error).toBe("清空失败");
  });
});
