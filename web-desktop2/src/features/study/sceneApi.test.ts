import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchScene } from "./sceneApi";

function reply(body: unknown): Response {
  return { ok: true, json: async () => body } as Response;
}

describe("fetchScene", () => {
  afterEach(() => vi.restoreAllMocks());

  it("returns null on non-ok", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({ ok: false, json: async () => ({}) } as Response);
    await expect(fetchScene("commit")).resolves.toBeNull();
  });

  it("passes word/pos/refresh and reads scene", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      reply({ scene: { word: "commit", en: "They commit to it.", zh: "他们投入其中。", source: "tatoeba" } }),
    );
    const got = await fetchScene("commit", "v.", "投入", true);
    expect(got?.source).toBe("tatoeba");
    expect(String(spy.mock.calls[0][0])).toContain("refresh=1");
    expect(String(spy.mock.calls[0][0])).toContain("pos=v.");
  });
});
