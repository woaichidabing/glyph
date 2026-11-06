/**
 * 启动时"该用哪个大脑"的决策（回归 BUG-02）。
 *
 * 曾经的教训：决策**缓存优先**——浏览器 localStorage 里存着哪个型号就用哪个，只要它还在
 * 型号清单里；若与后端不一致，还会把缓存里的型号 POST 回后端。结果：一次降级到 kimi-k3
 * 被写进缓存后，每次加载都被回推，反复覆盖用户指定的 qwen3.8-flash（2026-10-12 真机复现）。
 *
 * 修正后的规矩：**后端（/api/config）是模型配置的唯一事实源**。启动一律采用后端型号 /
 * 档位，缓存只用于"后端返回前先显示"（那一步在 useBrain 里做，不在这里）。只有后端型号
 * 或档位**本身非法**（下架 / 脏值）时才纠偏，且此时才需要把纠偏结果同步回后端。
 */

import { describe, expect, it } from "vitest";
import { effortShortLabel } from "./brainRepo";
import type { BrainModelsResponse } from "./configRepo";
import { resolveInitialChoice } from "./brainRepo";

const CATALOG: BrainModelsResponse = {
  default: "qwen3.8-27b",
  models: [
    {
      id: "qwen3.8-flash",
      label: "qwen3.8-flash",
      desc: "",
      reasoning_levels: ["low", "medium", "high"],
      web_search: false,
      context_window: 300000,
      brand: "",
      supports_vision: true,
    },
    {
      id: "qwen3.8-27b",
      label: "qwen3.8-27b",
      desc: "",
      reasoning_levels: ["medium", "high"],
      web_search: false,
      context_window: 1000000,
      brand: "",
      supports_vision: true,
    },
    {
      id: "kimi-k3",
      label: "kimi-k3",
      desc: "",
      reasoning_levels: ["low", "medium", "high"],
      web_search: true,
      context_window: 256000,
      brand: "",
      supports_vision: false,
    },
  ],
};

describe("resolveInitialChoice：后端是唯一事实源（BUG-02）", () => {
  it("后端型号合法 → 直接采用后端，不需要同步（缓存里的降级模型不许粘住）", () => {
    // 场景还原：浏览器缓存曾被写成 kimi-k3，后端用户已改回 qwen3.8-flash。
    // 旧实现会返回 kimi-k3 且 needsSync=true（把降级模型回推）；新实现只认后端。
    const decision = resolveInitialChoice({
      catalog: CATALOG,
      backend: { model: "qwen3.8-flash", effort: "medium" },
    });

    expect(decision.choice).toEqual({ model: "qwen3.8-flash", effort: "medium" });
    expect(decision.needsSync).toBe(false);
  });

  it("后端档位在合法取值内 → 原样采用", () => {
    const decision = resolveInitialChoice({
      catalog: CATALOG,
      backend: { model: "qwen3.8-27b", effort: "high" },
    });
    expect(decision.choice).toEqual({ model: "qwen3.8-27b", effort: "high" });
    expect(decision.needsSync).toBe(false);
  });

  it("后端型号不在清单里（下架 / 脏值）→ 用清单默认型号纠偏，并需要同步回后端", () => {
    const decision = resolveInitialChoice({
      catalog: CATALOG,
      backend: { model: "totally-fake-model", effort: "medium" },
    });
    expect(decision.choice.model).toBe("qwen3.8-27b");
    expect(decision.needsSync).toBe(true);
  });

  it("后端档位对该型号非法 → 落到该型号可用档，并需要同步回后端", () => {
    // qwen3.8-27b 不支持 low，后端却是 low：纠偏到 medium 并同步
    const decision = resolveInitialChoice({
      catalog: CATALOG,
      backend: { model: "qwen3.8-27b", effort: "low" },
    });
    expect(decision.choice).toEqual({ model: "qwen3.8-27b", effort: "medium" });
    expect(decision.needsSync).toBe(true);
  });

  it("后端不支思考的型号（levels 空）→ 档位回中档兜底，不抛错", () => {
    const noThink: BrainModelsResponse = {
      default: "text-only",
      models: [
        {
          id: "text-only",
          label: "text-only",
          desc: "",
          reasoning_levels: [],
          web_search: false,
          context_window: 128000,
          brand: "",
          supports_vision: false,
        },
      ],
    };
    const decision = resolveInitialChoice({
      catalog: noThink,
      backend: { model: "text-only", effort: "high" },
    });
    expect(decision.choice.model).toBe("text-only");
    expect(decision.choice.effort).toBe("medium");
  });
});

/* 框外那颗「思考强度」chip 上只放一个字（低 / 中 / 高）。
   effortLabel 返回的可能是空串（不支持思考），那颗 chip 整个不渲染，所以这里不用管空。 */
describe("effortShortLabel · 强度 chip 上的短标签", () => {
  it("三档各给一个字", () => {
    expect(effortShortLabel("low")).toBe("低");
    expect(effortShortLabel("medium")).toBe("中");
    expect(effortShortLabel("high")).toBe("高");
  });
  it("脏值给空串，调用方据此不渲染 chip", () => {
    expect(effortShortLabel("bogus")).toBe("");
    expect(effortShortLabel(null)).toBe("");
    expect(effortShortLabel(undefined)).toBe("");
  });
});
