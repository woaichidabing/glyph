import { describe, it, expect } from "vitest";
import {
  effectiveWindow,
  normalizeContextMode,
  supportsMaxContext,
  STANDARD_WINDOW,
} from "./contextPrefRepo";

describe("normalizeContextMode 只认两档", () => {
  it("合法档原样，脏值回标准", () => {
    expect(normalizeContextMode("max")).toBe("max");
    expect(normalizeContextMode(" MAX ")).toBe("max");
    expect(normalizeContextMode("ultra")).toBe("standard");
    expect(normalizeContextMode(undefined)).toBe("standard");
  });
});

describe("effectiveWindow 不超型号真实窗口", () => {
  it("标准 / Max 取档位上限与型号窗口的较小值", () => {
    expect(effectiveWindow("max", 1_000_000)).toBe(1_000_000);
    expect(effectiveWindow("standard", 1_000_000)).toBe(STANDARD_WINDOW);
    expect(effectiveWindow("max", 128_000)).toBe(128_000);
    // 型号窗口缺失时按档位目标，不返回 0
    expect(effectiveWindow("max", undefined)).toBe(1_000_000);
  });
});

describe("supportsMaxContext 按型号窗口决定要不要显「更大上下文」", () => {
  it("窗口超过 30 万标准档才有 Max 增益", () => {
    expect(supportsMaxContext(1_000_000)).toBe(true);
    expect(supportsMaxContext(512_000)).toBe(true);
  });
  it("窗口不超过 30 万（128k/200k 等）隐藏 Max 开关", () => {
    expect(supportsMaxContext(128_000)).toBe(false);
    expect(supportsMaxContext(STANDARD_WINDOW)).toBe(false);
  });
  it("窗口未知（未就绪 / 后端没给）默认显示，避免误藏内置模型的开关", () => {
    expect(supportsMaxContext(undefined)).toBe(true);
    expect(supportsMaxContext(0)).toBe(true);
  });
});
