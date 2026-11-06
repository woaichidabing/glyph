import { afterEach, describe, expect, it, vi } from "vitest";
import { loadStudyState, saveStudyState } from "./studyApi";

describe("studyApi", () => {
  afterEach(() => vi.restoreAllMocks());

  it("reads remote state", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({ state: { settings: {} } }) } as Response);
    await expect(loadStudyState()).resolves.toEqual({ settings: {} });
  });

  it("throws instead of returning null when backend fails", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false, json: async () => ({}) } as Response);
    await expect(loadStudyState()).rejects.toThrow("背词状态没读上");
  });

  it("puts state", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: true, json: async () => ({ saved: true }) } as Response);
    await saveStudyState({ settings: {} });
    expect(spy.mock.calls[0][1]?.method).toBe("PUT");
    expect(spy.mock.calls[0][1]?.keepalive).toBe(true);
  });
});
