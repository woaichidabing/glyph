import { describe, it, expect } from "vitest";
import {
  badgeLetter,
  brandForModel,
  logoForBrand,
  logoForModel,
  resolveBrand,
} from "./brainLogo";

describe("brandForModel 按型号名判定品牌", () => {
  it.each([
    ["gpt-4o", "openai"],
    ["o3-mini", "openai"],
    ["claude-3-5-sonnet", "anthropic"],
    ["gemini-2.0-flash", "google"],
    ["grok-3", "xai"],
    ["deepseek-chat", "deepseek"],
    ["qwen3.8-27b", "qwen"],
    ["glm-5.3", "glm"],
    ["kimi-k3", "kimi"],
  ])("%s -> %s", (id, want) => {
    expect(brandForModel(id)).toBe(want);
  });

  it("认不得的型号归 generic（交给首字母徽章），空串归 unknown", () => {
    expect(brandForModel("some-weird-inhouse-llm")).toBe("generic");
    expect(brandForModel("")).toBe("unknown");
    expect(brandForModel(null)).toBe("unknown");
  });
});

describe("resolveBrand 以后端下发 brand 优先、id 判定兜底", () => {
  it("后端给了合法品牌就用后端的", () => {
    expect(resolveBrand("anthropic", "totally-custom-name")).toBe("anthropic");
  });
  it("智谱 zhipu 归一到 glm（共用官方 logo）", () => {
    expect(resolveBrand("zhipu", "glm-something")).toBe("glm");
  });
  it("后端 brand 缺失 / 非法时回退到型号名判定", () => {
    expect(resolveBrand(undefined, "gpt-4o-mini")).toBe("openai");
    expect(resolveBrand("nonsense", "grok-3")).toBe("xai");
    expect(resolveBrand(undefined, "xyz")).toBe("generic");
  });
});

describe("logoForBrand 只给内置官方图，其余交首字母徽章", () => {
  it("内置四家返回非空图片", () => {
    for (const b of ["qwen", "deepseek", "glm", "kimi"] as const) {
      expect(logoForBrand(b)).not.toBe("");
    }
  });
  it("OpenAI/Claude/Gemini/Grok/通用没有内置图，返回空串（不硬配错图）", () => {
    for (const b of ["openai", "anthropic", "google", "xai", "generic", "unknown"] as const) {
      expect(logoForBrand(b)).toBe("");
    }
  });
  it("logoForModel 按型号名给出内置图或空串", () => {
    expect(logoForModel("qwen3.8-27b")).not.toBe("");
    expect(logoForModel("gpt-4o")).toBe("");
  });
});

describe("badgeLetter 首字母徽章", () => {
  it("取型号名首字母并大写", () => {
    expect(badgeLetter("gpt-4o")).toBe("G");
    expect(badgeLetter("claude-3")).toBe("C");
    expect(badgeLetter(" deepseek")).toBe("D");
  });
  it("空名给中性占位", () => {
    expect(badgeLetter("")).toBe("?");
  });
});
