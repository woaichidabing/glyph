/* ============================================================
 * 外观设置：主题 / 界面密度 / 减少动效 / 消息字号。
 *
 * 为什么单独一个模块：这几件事的**判断逻辑**（默认值、合法性、属性映射）
 * 跟 zustand store 没关系，拆出来就能脱离 DOM 测。
 * store.ts 只负责"读 localStorage → parseAppearance → applyAppearance"这条线。
 *
 * 依据：web-desktop2/design-lab/transcript-cc-parity-templates.html 的设置弹窗（已定稿）
 * ============================================================ */

export type ThemeMode = "light" | "dark" | "auto";
/** 界面密度：紧凑把行距和内边距收一档，小屏笔记本一屏能多放些内容 */
export type Density = "comfortable" | "compact";
/** 消息字号。**只作用于消息正文**，不挂到整个界面上（照 Claude 桌面端） */
export type TextSize = "sm" | "md" | "lg";
export type EnglishFont = "sans" | "serif";
export type LineWidth = "narrow" | "standard" | "wide";
export type LineHeight = "compact" | "comfortable" | "loose";
export type MarkStyle = "underline" | "highlight" | "high";

export interface Appearance {
  mode: ThemeMode;
  density: Density;
  reduceMotion: boolean;
  textSize: TextSize;
  englishFont: EnglishFont;
  lineWidth: LineWidth;
  lineHeight: LineHeight;
  markStyle: MarkStyle;
}

export const DEFAULT_APPEARANCE: Appearance = {
  mode: "auto",
  density: "comfortable",
  reduceMotion: false,
  textSize: "md",
  englishFont: "sans",
  lineWidth: "standard",
  lineHeight: "comfortable",
  markStyle: "underline",
};

const MODES = ["light", "dark", "auto"] as const;
const DENSITIES = ["comfortable", "compact"] as const;
const TEXT_SIZES = ["sm", "md", "lg"] as const;
const ENGLISH_FONTS = ["sans", "serif"] as const;
const LINE_WIDTHS = ["narrow", "standard", "wide"] as const;
const LINE_HEIGHTS = ["compact", "comfortable", "loose"] as const;
const MARK_STYLES = ["underline", "highlight", "high"] as const;

function pick<T extends string>(v: unknown, allowed: readonly T[], fallback: T): T {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : fallback;
}

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined"
    && !!window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

/** auto 按系统解析成实际的 light / dark */
export function resolveTheme(mode: ThemeMode): "light" | "dark" {
  return mode === "auto" ? (systemPrefersDark() ? "dark" : "light") : mode;
}

/**
 * 把外观算成一组 <html data-*> 属性。纯函数，好测。
 * 界面上的开关是「动画效果」（开=有动画，默认开）。内部仍存 reduceMotion：
 * 关着时也写 full，不是删掉属性 —— CSS 要能分清「没开」和「没设置」。
 */
export function appearanceAttrs(a: Appearance): Record<string, string> {
  return {
    panel: resolveTheme(a.mode),
    density: a.density,
    motion: a.reduceMotion ? "reduced" : "full",
    textsize: a.textSize,
    enfont: a.englishFont,
    linewidth: a.lineWidth,
    lineheight: a.lineHeight,
    markstyle: a.markStyle,
  };
}

/**
 * 从 localStorage 读出来的原始字符串还原成外观。
 *
 * · 坏 JSON / 缺字段 / 非法枚举值一律回落默认 —— **localStorage 是可以被手改的，不能信**
 * · `legacyMode` 是迁移兜底：以前主题单独存在 `markr.theme` 里（一个裸字符串），
 *   新键在时以新键为准，新键不在时才回头看它
 */
export function parseAppearance(raw: string | null, legacyMode?: string | null): Appearance {
  let parsed: unknown = null;
  if (raw) {
    try { parsed = JSON.parse(raw); } catch { parsed = null; }
  }
  const o = (parsed && typeof parsed === "object" ? parsed : {}) as Record<string, unknown>;
  const hasNewMode = typeof o.mode === "string" && (MODES as readonly string[]).includes(o.mode);
  return {
    mode: hasNewMode ? pick(o.mode, MODES, DEFAULT_APPEARANCE.mode) : pick(legacyMode, MODES, DEFAULT_APPEARANCE.mode),
    density: pick(o.density, DENSITIES, DEFAULT_APPEARANCE.density),
    reduceMotion:
      typeof o.reduceMotion === "boolean" ? o.reduceMotion : DEFAULT_APPEARANCE.reduceMotion,
    textSize: pick(o.textSize, TEXT_SIZES, DEFAULT_APPEARANCE.textSize),
    englishFont: pick(o.englishFont, ENGLISH_FONTS, DEFAULT_APPEARANCE.englishFont),
    lineWidth: pick(o.lineWidth, LINE_WIDTHS, DEFAULT_APPEARANCE.lineWidth),
    lineHeight: pick(o.lineHeight, LINE_HEIGHTS, DEFAULT_APPEARANCE.lineHeight),
    markStyle: pick(o.markStyle, MARK_STYLES, DEFAULT_APPEARANCE.markStyle),
  };
}

/** 落到 <html> 上。薄薄一层，不做判断（判断都在上面两个纯函数里）。 */
export function applyAppearance(a: Appearance): void {
  const root = document.documentElement;
  for (const [k, v] of Object.entries(appearanceAttrs(a))) root.dataset[k] = v;
}
