/**
 * 生词本 / 查词历史后端接口（/api/user/wordbook*）的适配层契约测试。
 *
 * 这一层只做三件事：拼路径、发 JSON、把后端契约原样交回。
 * 非 2xx 必须抛错（detail 优先，没有 detail 就给状态码），
 * 否则 store 会把失败当成功、乐观更新永远不回滚。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import type { DictEntry, HistoryItem, WordbookItem } from "../../types";
import {
  addWord,
  clearHistory,
  clearWords,
  getState,
  importState,
  recordLookup,
  removeWord,
  restoreWord,
} from "./userWordbookApi";

const ITEM: WordbookItem = {
  word: "commit",
  addedAt: 1_700_000_000_000,
  phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
  gloss: "承诺，犯（罪）",
};

const HIST: HistoryItem = { word: "commit", at: 1_700_000_000_000, count: 2 };

const ENTRY: DictEntry = {
  word: "commit",
  phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
  senses: [{ pos: "v.", translation: "承诺，犯（罪）", examples: [] }],
  source: "api",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("userWordbookApi · 成功解析", () => {
  it("getState 取 {items, history}，走相对 /api 路径", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ items: [ITEM], history: [HIST] }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const snap = await getState();

    expect(snap).toEqual({ items: [ITEM], history: [HIST] });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/user/wordbook");
    // GET 不带 init，别把 undefined 当第二个参数传出去
    expect(fetchMock.mock.calls[0][1]).toBeUndefined();
  });

  it("addWord 把整条词条放进 {entry}，并交回 item 与快照", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ items: [ITEM], history: [], item: ITEM }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const out = await addWord(ENTRY);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/user/wordbook");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ entry: ENTRY });
    expect(out.item).toEqual(ITEM);
    expect(out.items).toEqual([ITEM]);
  });

  it("removeWord 的词做 URL 编码，DELETE 到 /api/user/wordbook/{word}", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ items: [], history: [], removed: ITEM }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const out = await removeWord("give up");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/user/wordbook/give%20up");
    expect(fetchMock.mock.calls[0][1]?.method).toBe("DELETE");
    expect(out.removed).toEqual(ITEM);
  });

  it("recordLookup 把词放进 {word}，POST 到 history", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ items: [ITEM], history: [HIST], item: HIST }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await recordLookup("commit");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/user/wordbook/history");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ word: "commit" });
  });

  it("restoreWord 发 {item}，clearWords / clearHistory 用 DELETE", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ items: [], history: [], item: ITEM }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await restoreWord(ITEM);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/user/wordbook/restore");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ item: ITEM });

    await clearWords();
    expect(fetchMock.mock.calls[1][0]).toBe("/api/user/wordbook");
    expect(fetchMock.mock.calls[1][1]?.method).toBe("DELETE");

    await clearHistory();
    expect(fetchMock.mock.calls[2][0]).toBe("/api/user/wordbook/history");
    expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");
  });
});

describe("userWordbookApi · import 体形状", () => {
  it("importState 发送 {items, history}，返回 imported 计数", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({
        items: [ITEM],
        history: [HIST],
        importedItems: 1,
        importedHistory: 1,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const out = await importState([ITEM], [HIST]);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/user/wordbook/import");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(String(init?.body))).toEqual({ items: [ITEM], history: [HIST] });
    expect(out.importedItems).toBe(1);
    expect(out.importedHistory).toBe(1);
  });
});

describe("userWordbookApi · 非 2xx 抛错", () => {
  it("优先抛后端 detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, _init?: RequestInit) =>
        jsonResponse({ detail: "生词本文件写不进去" }, 500),
      ),
    );

    await expect(getState()).rejects.toThrow("生词本文件写不进去");
  });

  it("没有 detail 时抛状态码，不吞错", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, _init?: RequestInit) => new Response("oops", { status: 503 })),
    );

    await expect(clearWords()).rejects.toThrow("503");
  });
});
