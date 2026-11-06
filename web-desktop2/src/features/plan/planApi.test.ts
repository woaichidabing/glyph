import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchPlan, planEvent, setPlanGoal } from "./planApi";

function reply(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

describe("planApi", () => {
  afterEach(() => vi.restoreAllMocks());

  it("maps backend ms doneAt to local date and keeps time", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      reply({
        version: 1,
        goal: null,
        tasks: [{
          id: "a", t: "背 20 个词", src: "glyph", kind: "study", target: 1,
          progress: 1, done: true, createdAt: "2026-10-22",
          doneAt: new Date(2026, 9, 22, 9, 12).getTime(), evidence: "今日背词完成",
        }],
      }),
    );
    const snap = await fetchPlan();
    expect(snap.tasks[0].doneAt).toBe("2026-10-22");
    expect(snap.tasks[0].doneAtMs).toBeTypeOf("number");
    expect(snap.tasks[0].kind).toBe("study");
  });

  it("posts goal and event to the same API", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      reply({ version: 1, goal: null, tasks: [], task: null }),
    );
    await setPlanGoal("三个月过六级");
    await planEvent("dict", "查过 commit");
    expect(spy.mock.calls[0][0]).toBe("/api/plan/goal");
    expect(spy.mock.calls[1][0]).toBe("/api/plan/event");
    expect(String(spy.mock.calls[1][1]?.body)).toContain("dict");
  });
});
