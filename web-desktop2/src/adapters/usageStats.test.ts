import { describe, expect, it } from "vitest";
import { bars, hitPct, savedPct, weekdayLabel, type UsageDay } from "./usageStats";

const day = (over: Partial<UsageDay> = {}): UsageDay => ({
  day: "2026-10-12",
  input_tokens: 15000,
  output_tokens: 100,
  cached_tokens: 14000,
  measured_input_tokens: 15000,
  turns: 3,
  ...over,
});

describe("命中率 / 省了多少", () => {
  it("分母只认带明细的那部分输入（老流水不能把比例压低）", () => {
    // 一天里既有老流水（30 万输入、没明细）又有新流水（1.5 万输入、命中 1.4 万）
    const row = day({
      input_tokens: 315000,
      measured_input_tokens: 15000,
      cached_tokens: 14000,
    });

    expect(hitPct(row)).toBe(93); // 14000 / 15000，而不是 14000 / 315000
    expect(savedPct(row)).toBe(75); // 0.8 × 93.3
  });

  it("没带明细时两个都是 null（未知，不谎报 0）", () => {
    const row = day({ cached_tokens: null, measured_input_tokens: 0 });
    expect(hitPct(row)).toBeNull();
    expect(savedPct(row)).toBeNull();
  });

  it("真 0 命中照样给 0%", () => {
    const row = day({ cached_tokens: 0, measured_input_tokens: 15000 });
    expect(hitPct(row)).toBe(0);
    expect(savedPct(row)).toBe(0);
  });
});

describe("柱子摆位", () => {
  it("最高的一天是 100%，深色段是命中缓存那截", () => {
    const out = bars([
      day({ day: "2026-10-11", input_tokens: 5000, cached_tokens: 2500, measured_input_tokens: 5000 }),
      day({ day: "2026-10-12", input_tokens: 20000, cached_tokens: 15000, measured_input_tokens: 20000 }),
    ]);

    expect(out[0].totalPct).toBe(25);
    expect(out[0].cachedPct).toBe(13);
    expect(out[1].totalPct).toBe(100);
    expect(out[1].cachedPct).toBe(75);
  });

  it("没有用量的天也占一格（柱子连续，不骗人）", () => {
    const out = bars([
      day({ day: "2026-10-11", input_tokens: 0, cached_tokens: null, measured_input_tokens: 0 }),
      day({ day: "2026-10-12", input_tokens: 100, cached_tokens: 50, measured_input_tokens: 100 }),
    ]);

    expect(out[0].totalPct).toBe(0);
    expect(out[0].cachedPct).toBe(0);
    expect(out[1].totalPct).toBe(100);
  });
});

describe("日期标签", () => {
  it("ISO 日期给星期几", () => {
    expect(weekdayLabel("2026-10-12")).toBe("周一");
    expect(weekdayLabel("2026-10-11")).toBe("周日");
  });

  it("解析不出来就照原样返回，不编", () => {
    expect(weekdayLabel("昨天")).toBe("昨天");
  });
});
