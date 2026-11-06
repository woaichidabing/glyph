import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => { map.set(k, String(v)); },
    removeItem: (k: string) => { map.delete(k); },
    clear: () => map.clear(),
  };
}

const today = new Date();
const date = today.getFullYear() + "-" + String(today.getMonth() + 1).padStart(2, "0") + "-" + String(today.getDate()).padStart(2, "0");

describe("study settings save", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage());
    localStorage.setItem("agent.study.settings.v1", JSON.stringify({
      bookId: "cet6", mode: "plan", dailyNew: 30, dailyReview: 60, groupSize: 5, autoSound: false,
    }));
    localStorage.setItem("agent.study.queue.v1", JSON.stringify({
      date, newWords: ["a", "b"], reviewWords: [], wrongWords: [],
      doneNew: 1, doneReview: 0, doneWrong: 0,
    }));
    localStorage.setItem("agent.study.cards.v1", "{}");
    localStorage.setItem("agent.study.errors.v1", "[]");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("autoSound-only save keeps today queue and done count", async () => {
    vi.resetModules();
    const { useStudy } = await import("./studyStore");
    useStudy.getState().patchSettings({ autoSound: true });
    const queue = JSON.parse(localStorage.getItem("agent.study.queue.v1") || "{}");
    expect(queue.doneNew).toBe(1);
    expect(queue.newWords).toEqual(["a", "b"]);
  });
});
