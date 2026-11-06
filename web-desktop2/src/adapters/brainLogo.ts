/**
 * 大脑品牌 logo 映射：先信后端按官方能力判定下发的 brand，缺失时再按模型 id 前缀猜。
 * 纯逻辑放在 adapter 里、组件只负责渲染（与 brainRepo 等其他 adapter 同一约定）。
 *
 * 只有内置四家（千问 / DeepSeek / 智谱 / Kimi）打包了官方标志图；OpenAI、Claude、
 * Gemini、Grok 以及认不得的型号不硬配错图，统一退到「品牌色首字母圆徽章」
 * （组件据 logoForBrand 返回空串来渲染徽章），绝不乱配一个 logo。
 */

import deepseekLogo from "../assets/logos/deepseek.png";
import glmLogo from "../assets/logos/glm.png";
import kimiLogo from "../assets/logos/kimi.png";
import qwenLogo from "../assets/logos/qwen.png";

export type BrainBrand =
  | "qwen"
  | "deepseek"
  | "glm"
  | "kimi"
  | "openai"
  | "anthropic"
  | "google"
  | "xai"
  | "generic"
  | "unknown";

const KNOWN_BRANDS: ReadonlySet<string> = new Set<BrainBrand>([
  "qwen",
  "deepseek",
  "glm",
  "kimi",
  "openai",
  "anthropic",
  "google",
  "xai",
  "generic",
]);

/** 按型号名前缀判定品牌（去空格、大小写容错）；认不得归 generic，空名归 unknown。 */
export function brandForModel(modelId: string | null | undefined): BrainBrand {
  const id = (modelId ?? "").trim().toLowerCase();
  if (!id) return "unknown";
  if (id.startsWith("glm")) return "glm";
  if (id.startsWith("deepseek")) return "deepseek";
  if (id.startsWith("kimi")) return "kimi";
  if (id.startsWith("qwen")) return "qwen";
  if (id.startsWith("claude")) return "anthropic";
  if (id.startsWith("gemini")) return "google";
  if (id.startsWith("grok")) return "xai";
  if (id.startsWith("gpt") || /^o[0-9]/.test(id)) return "openai";
  return "generic";
}

/** 以后端下发 brand 为准（智谱 zhipu 归一到 glm 共用标志），缺失 / 非法再按型号名猜。 */
export function resolveBrand(
  hint: string | null | undefined,
  modelId: string | null | undefined,
): BrainBrand {
  const h = (hint ?? "").trim().toLowerCase();
  const normalized = h === "zhipu" ? "glm" : h;
  if (normalized && KNOWN_BRANDS.has(normalized)) return normalized as BrainBrand;
  return brandForModel(modelId);
}

const LOGO_BY_BRAND: Partial<Record<BrainBrand, string>> = {
  qwen: qwenLogo,
  deepseek: deepseekLogo,
  glm: glmLogo,
  kimi: kimiLogo,
};

/** 取某品牌的官方标志图；没有内置图（OpenAI/Claude/Gemini/Grok/通用）返回空串。 */
export function logoForBrand(brand: BrainBrand | string | null | undefined): string {
  return LOGO_BY_BRAND[(brand ?? "") as BrainBrand] ?? "";
}

/** 取某型号的品牌 logo（仅按型号名）；未知型号返回空串，组件据此渲染首字母徽章。 */
export function logoForModel(modelId: string | null | undefined): string {
  return logoForBrand(brandForModel(modelId));
}

/** 首字母圆徽章上显示的字母：型号名首字母大写；空名给中性问号。 */
export function badgeLetter(modelId: string | null | undefined): string {
  const id = (modelId ?? "").trim();
  const ch = id.charAt(0);
  return ch ? ch.toUpperCase() : "?";
}
