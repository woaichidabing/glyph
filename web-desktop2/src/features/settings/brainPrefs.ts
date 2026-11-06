/**
 * 推理强度 / 上下文档位的人话文案与展示口径（设置页用）。
 *
 * 只放**文案与格式化**，不放请求、不放状态——行为仍然走 `configRepo`
 * （`saveBrainChoice` / `saveContextMode`）与 `contextPrefRepo`（窗口算式），
 * 这个文件的存在是为了让"同一个档位在两处说同一种话"。
 *
 * ⚠️ 已知待统一：`BrainPicker.tsx` 里有同义的 `LEVEL_DESC` / `CTX_DESC`
 * 局部常量（侧栏浮层用紧凑版）。该文件当前在另一个 AI 的在途改动中，先不动；
 * 等它落定后把那两处改为 import 本文件，浮层与设置页即共用同一份说法。
 */

import { effectiveWindow, type ContextMode } from "./contextPrefRepo";
import type { Effort } from "./brainRepo";

/** 档位的固定展示顺序（与后端词表一致：low→medium→high→xhigh→max）；默认只到 high */
export const EFFORT_ORDER: Effort[] = ["off", "low", "medium", "high", "xhigh", "max"];

/** 各档说明：设置页版（比侧栏浮层多一句"什么时候该选它"）；极高/最高只有模型声明支持时才出现 */
export const EFFORT_DESC: Record<Effort, string> = {
  off: "关掉思考：最快最省，适合闲聊和简单问答。",
  low: "回得快、省额度。改个句子、查个词这类小事够用。",
  medium: "快慢和准确度取中。日常批改、讲题、练习选这档。",
  high: "先多想一会儿再答，难题更稳，但明显更慢、更耗额度。",
  xhigh: "长文推理、复杂纠错再往上推一档；更慢，也更耗额度。",
  max: "最慢最贵，留给最难的题；厂商不支持这一档时会直接报错。",
};

/** 上下文档位：只讲取舍，数字由 windowText 按型号真实窗口算，不写死 */
export const CONTEXT_DESC: Record<ContextMode, string> = {
  standard: "更早整理前文，更省额度、响应也更快。日常对话、作文、练习够用。",
  max: "能装下更长对话和整份材料，少丢前文，但更耗额度、响应会慢一些。",
};

/**
 * token 数转"多少万"的中文口径（30 万 / 12.8 万）。
 * 0 或空回空串——窗口未知时界面不显示数字，不显示假的。
 */
export function windowText(tokens: number | undefined | null): string {
  const n = Number(tokens);
  if (!Number.isFinite(n) || n <= 0) return "";
  if (n < 10_000) return String(n);
  const wan = n / 10_000;
  return `${Number.isInteger(wan) ? wan : wan.toFixed(1)} 万`;
}

/**
 * 某型号在某档位下的实际窗口文案（如"30 万"）。
 * 走 `effectiveWindow`——标称窗口小的模型开 Max 也不会变大，数字必须跟着算，
 * 不能写死 30 万 / 100 万。
 */
export function windowTextFor(mode: ContextMode, modelWindow?: number): string {
  return windowText(effectiveWindow(mode, modelWindow));
}
