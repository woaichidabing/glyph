import { describe, expect, it } from "vitest";
import { homeAllDone, pickPrimary, type HomeBlock } from "./studyHome";

const b = (kind: HomeBlock["kind"], total: number, done: number): HomeBlock => ({ kind, total, done });

describe("study home gating", () => {
  it("moves from finished new to review", () => {
    const blocks = [b("new", 2, 2), b("review", 3, 0), b("wrong", 0, 0)];
    expect(homeAllDone(blocks, false)).toBe(false);
    expect(pickPrimary(blocks, false, "new")?.kind).toBe("review");
  });

  it("keeps wrong words as unfinished work", () => {
    const blocks = [b("new", 2, 2), b("review", 3, 3), b("wrong", 2, 0)];
    expect(homeAllDone(blocks, false)).toBe(false);
    expect(pickPrimary(blocks, false, "new")?.kind).toBe("wrong");
  });

  it("finishes only when all three queues are done", () => {
    expect(homeAllDone([b("new", 2, 2), b("review", 3, 3), b("wrong", 2, 2)], false)).toBe(true);
  });

  it("never says all done in free mode", () => {
    const blocks = [b("new", 2, 2), b("review", 3, 3), b("wrong", 0, 0)];
    expect(homeAllDone(blocks, true)).toBe(false);
    expect(pickPrimary(blocks, true, "review")?.kind).toBe("new");
  });
});
