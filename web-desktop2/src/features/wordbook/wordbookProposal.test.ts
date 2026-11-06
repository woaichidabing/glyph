import { describe, it, expect } from "vitest";
import { extractWordbookProposal } from "./wordbookProposal";

const base = {
  proposed: true,
  status: "proposed",
  word: "commit",
  phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
  pos: "v.",
  gloss: "承诺；投入；犯（错）",
  lookedUpCount: 4,
  summary: "卡片已交给界面",
};

describe("extractWordbookProposal", () => {
  it("解析收词卡需要的最小字段", () => {
    const got = extractWordbookProposal(base);
    expect(got).toEqual({
      word: "commit",
      phonetic: { uk: "/kəˈmɪt/", us: "/kəˈmɪt/" },
      pos: "v.",
      gloss: "承诺；投入；犯（错）",
      lookedUpCount: 4,
    });
  });

  it("容忍 JSON 字符串（历史回放 / SSE 两种形状）", () => {
    expect(extractWordbookProposal(JSON.stringify(base))?.word).toBe("commit");
  });

  it("没有音标 / 词性也照常出卡", () => {
    const got = extractWordbookProposal({ ...base, phonetic: {}, pos: "" });
    expect(got?.word).toBe("commit");
    expect(got?.phonetic).toBeUndefined();
    expect(got?.pos).toBe("");
  });

  it("词形变化时带回 queriedForm", () => {
    const got = extractWordbookProposal({ ...base, word: "go", queriedForm: "went" });
    expect(got?.queriedForm).toBe("went");
  });

  it("proposed 不为 true 时不出卡（not_found / already_collected 等）", () => {
    expect(extractWordbookProposal({ ...base, proposed: false })).toBeNull();
    expect(extractWordbookProposal({ ...base, status: "not_found" })).toBeNull();
  });

  it("缺词或释义为空时不出卡", () => {
    expect(extractWordbookProposal({ ...base, word: "  " })).toBeNull();
    expect(extractWordbookProposal({ ...base, gloss: "  " })).toBeNull();
  });

  it("次数坏值按 0 处理，负数不往下传", () => {
    expect(extractWordbookProposal({ ...base, lookedUpCount: "很多" })?.lookedUpCount).toBe(0);
    expect(extractWordbookProposal({ ...base, lookedUpCount: -2 })?.lookedUpCount).toBe(0);
    expect(extractWordbookProposal({ ...base, lookedUpCount: 3.9 })?.lookedUpCount).toBe(3);
  });

  it("null / 非对象 / 坏 JSON / 缺字段返回 null（不抛错）", () => {
    expect(extractWordbookProposal(null)).toBeNull();
    expect(extractWordbookProposal(undefined)).toBeNull();
    expect(extractWordbookProposal("not json")).toBeNull();
    expect(extractWordbookProposal({})).toBeNull();
    expect(extractWordbookProposal([])).toBeNull();
  });
});
