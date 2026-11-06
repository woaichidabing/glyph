import { describe, expect, it } from "vitest";
import {
  DEFAULT_APPEARANCE,
  appearanceAttrs,
  parseAppearance,
  resolveTheme,
  type Appearance,
} from "./appearance";

/**
 * 外观设置：主题 / 界面密度 / 减少动效 / 消息字号。
 *
 * 这两件事刻意做成**不碰 DOM 的纯函数**，好测：
 *   · appearanceAttrs —— 把外观算成一组 <html data-*> 属性
 *   · parseAppearance —— 从 localStorage 读出来的原始字符串还原成外观（含默认值与合法性校验）
 * 真正的 applyAppearance 只负责把属性贴到 documentElement 上，不做判断。
 *
 * 依据：web-desktop2/design-lab/transcript-cc-parity-templates.html 的设置弹窗（已定稿）。
 */

describe("resolveTheme · 主题解析", () => {
  it("light / dark 原样返回", () => {
    expect(resolveTheme("light")).toBe("light");
    expect(resolveTheme("dark")).toBe("dark");
  });

  it("auto 在拿不到系统偏好时回落 light（node 环境没有 window）", () => {
    expect(resolveTheme("auto")).toBe("light");
  });
});

describe("appearanceAttrs · 外观 → <html data-*> 属性", () => {
  it("四项都映射到对的属性名和值", () => {
    const a: Appearance = {
      mode: "dark", density: "compact", reduceMotion: true, textSize: "lg",
      englishFont: "serif", lineWidth: "wide", lineHeight: "loose", markStyle: "high",
    };
    expect(appearanceAttrs(a)).toEqual({
      panel: "dark",
      density: "compact",
      motion: "reduced",
      textsize: "lg",
      enfont: "serif",
      linewidth: "wide",
      lineheight: "loose",
      markstyle: "high",
    });
  });

  it("减少动效关着时写 full（不是删掉属性——CSS 要能分清「没开」和「没设置」）", () => {
    const a: Appearance = {
      mode: "light", density: "comfortable", reduceMotion: false, textSize: "sm",
      englishFont: "sans", lineWidth: "standard", lineHeight: "comfortable", markStyle: "underline",
    };
    expect(appearanceAttrs(a).motion).toBe("full");
  });

  it("主题是 auto 时，panel 落成系统解析后的实色值", () => {
    const a: Appearance = {
      mode: "auto", density: "comfortable", reduceMotion: false, textSize: "md",
      englishFont: "sans", lineWidth: "standard", lineHeight: "comfortable", markStyle: "underline",
    };
    expect(appearanceAttrs(a).panel).toBe("light");
  });
});

describe("parseAppearance · 从 localStorage 还原", () => {
  it("什么都没存 → 默认值", () => {
    expect(parseAppearance(null)).toEqual(DEFAULT_APPEARANCE);
  });

  it("存了完整 JSON → 原样解析", () => {
    const raw = JSON.stringify({
      mode: "dark", density: "compact", reduceMotion: true, textSize: "lg",
      englishFont: "serif", lineWidth: "wide", lineHeight: "loose", markStyle: "high",
    });
    expect(parseAppearance(raw)).toEqual({
      mode: "dark", density: "compact", reduceMotion: true, textSize: "lg",
      englishFont: "serif", lineWidth: "wide", lineHeight: "loose", markStyle: "high",
    });
  });

  it("坏 JSON 不炸 → 默认值", () => {
    expect(parseAppearance("{ 这不是 json")).toEqual(DEFAULT_APPEARANCE);
    expect(parseAppearance("")).toEqual(DEFAULT_APPEARANCE);
  });

  it("字段缺失 → 缺的那几个补默认，有的保留", () => {
    expect(parseAppearance(JSON.stringify({ density: "compact" }))).toEqual({
      ...DEFAULT_APPEARANCE, density: "compact",
    });
  });

  it("非法枚举值不认，回落默认（localStorage 是可以被手改的，不能信）", () => {
    const raw = JSON.stringify({
      mode: "rainbow", density: "超紧凑", reduceMotion: "是", textSize: 42,
      englishFont: "comic", lineWidth: "无限", lineHeight: "巨大", markStyle: "彩虹",
    });
    expect(parseAppearance(raw)).toEqual(DEFAULT_APPEARANCE);
  });

  it("reduceMotion 只认布尔值，不认字符串", () => {
    expect(parseAppearance(JSON.stringify({ reduceMotion: "true" })).reduceMotion).toBe(false);
    expect(parseAppearance(JSON.stringify({ reduceMotion: true })).reduceMotion).toBe(true);
  });

  it("兼容老键：新键没有、老键是合法主题时，主题沿用老键", () => {
    expect(parseAppearance(null, "dark").mode).toBe("dark");
    expect(parseAppearance(null, "auto").mode).toBe("auto");
  });

  it("兼容老键：老键是垃圾值时不影响其余默认", () => {
    expect(parseAppearance(null, "紫罗兰")).toEqual(DEFAULT_APPEARANCE);
    expect(parseAppearance(null, null)).toEqual(DEFAULT_APPEARANCE);
  });

  it("新键在时，以新键为准（老键只是迁移兜底）", () => {
    const raw = JSON.stringify({ mode: "light" });
    expect(parseAppearance(raw, "dark").mode).toBe("light");
  });
});

describe("DEFAULT_APPEARANCE · 默认值", () => {
  it("主题跟系统、密度舒适、动效正常、字号中", () => {
    expect(DEFAULT_APPEARANCE).toEqual({
      mode: "auto", density: "comfortable", reduceMotion: false, textSize: "md",
      englishFont: "sans", lineWidth: "standard", lineHeight: "comfortable", markStyle: "underline",
    });
  });
});
