/**
 * 精读作答接口的**解包契约**测试（回归 BUG-01）。
 *
 * 后端这四个端点的响应统一是信封 `{ ok: true, attempt: {...} }`
 * （见 `web/server.py` 的 create/get/patch/submit），而 UI 需要的是**里面那个 attempt**。
 * 一旦把信封整个交给界面，`attempt.notes.filter` 就会崩（2026-10-12 真机七选五 100% 复现）。
 * 这里把"必须解包、且坏形态不许把 undefined 交给界面"钉死在 adapter 边界上。
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReadingAttempt } from "./reading";
import {
  createAttempt,
  getAttempt,
  patchAttempt,
  submitAttempt,
} from "./readingAttempts";

/** 一份最小但字段形状真实的作答记录（含 notes / questions 数组） */
const ATTEMPT = {
  attempt_id: "attempt-1",
  quiz_id: "2024-xkb1-matching",
  qtype: "七选五",
  stage: "draft",
  updated_at: "2026-10-12T10:00:00+08:00",
  questions: [{ question_id: "q1", answer: "A" }],
  notes: [{ id: "n1", kind: "word", body: "生词" }],
} as unknown as ReadingAttempt;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("attempt 类接口把后端信封解包（BUG-01）", () => {
  it("createAttempt 交出的是 attempt 本身，不是 {ok,attempt} 信封", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true, attempt: ATTEMPT })));
    const out = await createAttempt({ quiz_id: "2024-xkb1-matching", qtype: "七选五" });
    expect(out.ok).toBe(true);
    if (out.ok) {
      expect(out.data).toEqual(ATTEMPT);
      // 界面要靠它画笔记锚点；拿到信封时这里是 undefined，正是崩溃点
      expect(Array.isArray(out.data.notes)).toBe(true);
    }
  });

  it("getAttempt 同样解包", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true, attempt: ATTEMPT })));
    const out = await getAttempt("attempt-1");
    expect(out.ok).toBe(true);
    if (out.ok) expect(out.data).toEqual(ATTEMPT);
  });

  it("patchAttempt 同样解包（作答/笔记的每次保存都走它）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true, attempt: ATTEMPT })));
    const out = await patchAttempt("attempt-1", { questions: [{ question_id: "q1", answer: "B" }] });
    expect(out.ok).toBe(true);
    if (out.ok) expect(out.data).toEqual(ATTEMPT);
  });

  it("submitAttempt 同样解包（交卷后进复盘也靠它）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true, attempt: ATTEMPT })));
    const out = await submitAttempt("attempt-1", { q1: "A" });
    expect(out.ok).toBe(true);
    if (out.ok) expect(out.data).toEqual(ATTEMPT);
  });

  it("信封里没有 attempt（坏形态）→ 人话错误，绝不把 undefined 交给界面", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true })));
    const out = await patchAttempt("attempt-1", {});
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.error).toContain("读不懂");
  });

  it("非 2xx 仍走后端 detail 人话（解包不改变错误路径）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "这次作答已经交卷了" }, 409)),
    );
    const out = await submitAttempt("attempt-1", {});
    expect(out.ok).toBe(false);
    if (!out.ok) expect(out.error).toBe("这次作答已经交卷了");
  });
});
