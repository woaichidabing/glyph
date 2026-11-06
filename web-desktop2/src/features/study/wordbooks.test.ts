import { describe, it, expect } from "vitest";
import {
  LOCAL_BOOKS,
  mergeStats,
  mergePage,
  takeWords,
  takeGloss,
  miniFallback,
  type StoredPage,
} from "./wordbooks";

describe("mergeStats：后端真实词量与本地词书元数据合并", () => {
  it("用后端 total 覆盖元数据，保留名称分组等展示信息", () => {
    const stats = [
      { tag: "zk", label: "中考", total: 1602 },
      { tag: "cet6", label: "CET6", total: 5407 },
    ];
    const books = mergeStats(LOCAL_BOOKS, stats);
    const cet6 = books.find((b) => b.id === "cet6");
    expect(cet6).toMatchObject({ id: "cet6", total: 5407, group: "大学" });
    expect(cet6?.name).toBeTruthy();
    expect(cet6?.cover).toBeTruthy();
    // 后端没返回的书（gre 不在 stats 里）标记为未同步，total=0，不伪造词量
    const gre = books.find((b) => b.id === "gre");
    expect(gre?.total).toBe(0);
  });

  it("后端不可用（stats=null）时全部 total=0，绝不拿宣传数冒充", () => {
    const books = mergeStats(LOCAL_BOOKS, null);
    expect(books).toHaveLength(LOCAL_BOOKS.length);
    expect(books.every((b) => b.total === 0)).toBe(true);
  });

  it("顺序固定按考试层级排列", () => {
    const books = mergeStats(LOCAL_BOOKS, [
      { tag: "gre", label: "GRE", total: 1 },
      { tag: "zk", label: "中考", total: 1 },
    ]);
    expect(books.map((b) => b.id)).toEqual(LOCAL_BOOKS.map((b) => b.id));
  });
});

describe("mergePage：分页词表缓存合并", () => {
  const p0 = (words: string[], next: number | null): StoredPage => ({
    total: 5, at: 1, pages: { 0: words }, next,
  });

  it("首页直接落缓存", () => {
    const merged = mergePage(null, {
      tag: "cet6", total: 5, cursor: 0, next_cursor: 2, words: ["a", "b"],
    });
    expect(merged.pages[0]).toEqual(["a", "b"]);
    expect(merged.next).toBe(2);
    expect(merged.total).toBe(5);
  });

  it("第二页拼到对应游标位置，total 以后端最新值为准", () => {
    const first = p0(["a", "b"], 2);
    const merged = mergePage(first, {
      tag: "cet6", total: 4, cursor: 2, next_cursor: null, words: ["c", "d"],
    });
    expect(merged.pages[0]).toEqual(["a", "b"]);
    expect(merged.pages[2]).toEqual(["c", "d"]);
    expect(merged.next).toBeNull();
    expect(merged.total).toBe(4);
  });

  it("重复拉同一页去重保序（重发不产生重复词）", () => {
    const first = p0(["a", "b"], 2);
    const merged = mergePage(first, {
      tag: "cet6", total: 5, cursor: 0, next_cursor: 2, words: ["a", "b"],
    });
    expect(merged.pages[0]).toEqual(["a", "b"]);
  });

  it("页内 entries 落成以词为键的轻量释义（学习卡干扰项用）", () => {
    const merged = mergePage(null, {
      tag: "cet6", total: 2, cursor: 0, next_cursor: null,
      words: ["State", "system"],
      entries: [
        { word: "State", phonetic: "steit", pos: "n.", gloss: "州, 状态" },
        { word: "system", phonetic: "", pos: "n.", gloss: "系统" },
      ],
    });
    expect(merged.gloss?.state).toEqual({ word: "State", phonetic: "steit", pos: "n.", gloss: "州, 状态" });
    expect(merged.gloss?.system?.gloss).toBe("系统");
  });

  it("旧缓存（无 gloss 字段）合并新页时不报错", () => {
    const old: StoredPage = { total: 2, at: 1, next: 2, pages: { 0: ["a"] } };
    const merged = mergePage(old, {
      tag: "cet6", total: 2, cursor: 2, next_cursor: null, words: ["b"],
      entries: [{ word: "b", phonetic: "", pos: "v.", gloss: "B 释义" }],
    });
    expect(merged.gloss?.b?.gloss).toBe("B 释义");
  });
});

describe("takeGloss：按连续前缀取词条（供学习卡出选择题）", () => {
  it("顺序与 takeWords 一致，缺页停在缺口前", () => {
    const cached: StoredPage = {
      total: 4, at: 1, next: null,
      pages: { 0: ["a", "b"], 2: ["c", "d"] },
      gloss: {
        a: { word: "a", phonetic: "", pos: "n.", gloss: "A" },
        b: { word: "b", phonetic: "", pos: "v.", gloss: "B" },
        c: { word: "c", phonetic: "", pos: "n.", gloss: "C" },
        d: { word: "d", phonetic: "", pos: "n.", gloss: "D" },
      },
    };
    const entries = takeGloss(cached, 10);
    expect(entries.map((e) => e.word)).toEqual(["a", "b", "c", "d"]);
    expect(entries[1]).toEqual({ word: "b", phonetic: "", pos: "v.", gloss: "B" });
  });

  it("没有 gloss 的旧缓存返回空数组（降级回 mini 出题）", () => {
    const cached: StoredPage = { total: 1, at: 1, next: null, pages: { 0: ["a"] } };
    expect(takeGloss(cached, 10)).toEqual([]);
  });

  it("gloss 缺失的词跳过（无释义做不了干扰项）", () => {
    const cached: StoredPage = {
      total: 2, at: 1, next: null,
      pages: { 0: ["a", "b"] },
      gloss: { a: { word: "a", phonetic: "", pos: "n.", gloss: "A" } },
    };
    expect(takeGloss(cached, 10).map((e) => e.word)).toEqual(["a"]);
  });
});

describe("takeWords：从分页缓存取从 0 开始的连续词", () => {
  it("按页顺序拼出前 limit 个", () => {
    const cached: StoredPage = {
      total: 4, at: 1, next: null,
      pages: { 0: ["a", "b"], 2: ["c", "d"] },
    };
    expect(takeWords(cached, 4)).toEqual(["a", "b", "c", "d"]);
    expect(takeWords(cached, 3)).toEqual(["a", "b", "c"]);
  });

  it("中间缺页只返回连续前缀，绝不跳过缺口造成漏词", () => {
    const cached: StoredPage = {
      total: 6, at: 1, next: 4,
      pages: { 0: ["a", "b"], 4: ["e", "f"] },
    };
    expect(takeWords(cached, 6)).toEqual(["a", "b"]);
  });

  it("无缓存返回空数组", () => {
    expect(takeWords(null, 600)).toEqual([]);
  });
});

describe("miniFallback：后端不可用时按标签从内置样例库取词", () => {
  it("按书的 miniTags 命中样例库标签", () => {
    const cet6 = LOCAL_BOOKS.find((b) => b.id === "cet6")!;
    const words = miniFallback(cet6, 600);
    expect(words.length).toBeGreaterThan(0);
    expect(words).toContain("ambiguous"); // mini 库中带 CET6 标签的样例
  });
});
