/**
 * BrainPicker 拆成两颗 chip 之后「chip 上写什么 / 要不要渲染」的三句纯判断。
 * 抽成纯函数是为了不挂 DOM 就能测（Vitest 跑在 node 环境里，没有 jsdom）。
 */
import { effortShortLabel } from "./brainRepo";

/** 模型 chip 上的字：只写型号名 —— 强度已经拆出去了，不再跟着型号一起显示。 */
export function modelChipText(input: {
  label: string | null;
  effort: string | null;
  ready: boolean;
}): string {
  if (input.label) return input.label;
  return input.ready ? "未配置模型" : "模型…";
}

/** 强度 chip 上的字：一个字（低 / 中 / 高）；空串表示这颗 chip 不该渲染。 */
export function effortChipText(effort: string | null | undefined): string {
  return effortShortLabel(effort);
}

/** 强度 chip 什么时候出现：型号支持思考（levels 非空）才出现，不是置灰。 */
export function shouldShowEffortChip(levels: readonly string[]): boolean {
  return levels.length > 0;
}
/* ---------- 思考深度滑杆（照定稿样稿 §5.6）----------
   位置换算抽成纯函数：拖拽 / 点轨道 / 方向键都只是喂它一个坐标，
   组件里不写算术，这样不用浏览器也能测。 */

/**
 * 第 i 档的滑柄落在轨道里的 x（像素）。
 * 两端各**内缩 inset**（样稿里量到的 11px）—— 滑柄是 16px 宽的方块，
 * 不内缩的话第一档 / 最后一档会有一半戳出轨道。
 */
export function railX(index: number, count: number, width: number, inset: number): number {
  const span = width - inset * 2;
  // 轨道窄到放不下两边内缩时，所有档位一起塌到左边 —— 不出负数、不除零
  if (count <= 1 || span <= 0) return inset;
  const i = Math.min(count - 1, Math.max(0, index));
  return inset + (i / (count - 1)) * span;
}

/**
 * 手指 / 鼠标落在 x 处，磁吸到最近的那一档（不是向下取整 —— 那样后段永远选不中）。
 * 轨道外面点也安全：先夹到 [0,1] 再乘。
 */
export function pickIndex(x: number, count: number, width: number, inset: number): number {
  if (count <= 1) return 0;
  const span = width - inset * 2;
  if (span <= 0) return 0;
  const p = Math.min(1, Math.max(0, (x - inset) / span));
  return Math.round(p * (count - 1));
}
/* ---------- 浮层高度 ----------
   原来写死 SHEET_H = 370，但模型列表限高 196 + 下面「更大上下文」那块才 350 上下，
   面板实际高度每次不一样。高度估错 → 「向上弹还是向下弹」判断错 → 面板被视口切掉，
   用户就滚不动（滚轮事件落在被切掉的区域外面）。 */

/** 模型浮层底部「更大上下文」那一块的大致高度（含开关、进度条、说明） */
const BOTTOM_BLOCK_H = 150;
/** 浮层上下内边距合计 */
const SHEET_PAD = 8;
/** 强度滑杆面板：固定高度 */
const LEVELS_SHEET_H = 150;
/** 上下文用量面板：固定高度 */
const CONTEXT_SHEET_H = 300;

/**
 * 这个浮层实际会占多高（给「向上弹还是向下弹」用）。
 * 只求够用，不追求像素级 —— 估高一点比估矮好：估矮会导致向下弹却放不下。
 */
export function sheetHeight(input: {
  view: "models" | "levels" | "context";
  listMaxH: number;
  hasBottomBlock: boolean;
}): number {
  if (input.view === "levels") return LEVELS_SHEET_H;
  if (input.view === "context") return CONTEXT_SHEET_H;
  return input.listMaxH + (input.hasBottomBlock ? BOTTOM_BLOCK_H : 0) + SHEET_PAD;
}
