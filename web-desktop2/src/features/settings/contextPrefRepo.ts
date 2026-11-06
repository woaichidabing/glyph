/**
 * 「最大上下文」档位的本地决策（纯函数 + localStorage）。
 *
 * 与 brainRepo 同一层：只负责"标准 / Max"两档的归一化、本地记忆与有效窗口计算，
 * 不依赖 React、不直接发请求。本地这份是"乐观值"：切换立即写入让用量环即时变，
 * 真正落后端（POST /api/config 的 context_mode）由 useBrain 负责同步、失败回滚到旧值；
 * 每次启动再用后端回传的档位校正本地，以后端为装配唯一配置源。
 *
 * 两档（产品拍板）：
 * - standard 标准：30 万 token。日常对话 / 作文 / 短练习够用，更早压缩、更省、上下文更聚焦；
 * - max：100 万 token。长对话、整本材料时再开，更晚压缩、少丢前文，但累计 token 更多。
 *
 * 四个在表大脑官方窗口都是 1M；有效窗口 = min(型号标称窗口, 档位上限)，绝不超过模型真实
 * 窗口。标准 30 万是产品取舍（刻意提前压缩），不是模型只能装 30 万。
 */
import { readJSON, writeJSON } from "../../adapters/localStore";

export type ContextMode = "standard" | "max";

export const CONTEXT_STORAGE_KEY = "glyph.contextMode";

export const STANDARD_WINDOW = 300_000;
export const MAX_WINDOW = 1_000_000;
export const DEFAULT_CONTEXT_MODE: ContextMode = "standard";

const MODES: readonly ContextMode[] = ["standard", "max"];

/** 只认 standard/max（大小写、首尾空格容错），undefined/null/拼错/脏值一律回退标准档。 */
export function normalizeContextMode(raw: unknown): ContextMode {
  if (typeof raw === "string") {
    const value = raw.trim().toLowerCase();
    if ((MODES as readonly string[]).includes(value)) return value as ContextMode;
  }
  return DEFAULT_CONTEXT_MODE;
}

/** 读本地档位；没存过 / 损坏 / 脏值时回退标准档。 */
export function readContextMode(): ContextMode {
  return normalizeContextMode(readJSON<unknown>(CONTEXT_STORAGE_KEY, null));
}

/** 写本地档位（先归一化，绝不存一个不存在的档）；隐私模式 / 配额满时静默降级。 */
export function writeContextMode(mode: ContextMode): void {
  writeJSON(CONTEXT_STORAGE_KEY, normalizeContextMode(mode));
}

/**
 * 某档位下、某型号的有效上下文窗口（用量环的分母 / 压缩阈值上限）。
 *
 * @param mode 标准 / Max
 * @param modelWindow 型号标称窗口（catalog 里的 context_window）；未知 / 未就绪给非正数
 * @returns min(型号标称, 档位上限)；型号标称缺失时直接用档位目标——四个在表型号都 1M，
 *          catalog 正常时结果一致，未就绪也先按档位显示，永不返回 0。
 */
export function effectiveWindow(mode: ContextMode, modelWindow?: number): number {
  const wanted = normalizeContextMode(mode) === "max" ? MAX_WINDOW : STANDARD_WINDOW;
  if (typeof modelWindow === "number" && Number.isFinite(modelWindow) && modelWindow > 0) {
    return Math.min(modelWindow, wanted);
  }
  return wanted;
}

/**
 * 某型号是否值得显示「更大上下文 Max」开关：只有标称窗口超过标准档（30 万）时，
 * 开 Max 才真能装下更多；128k / 200k 这类小窗口模型开了也没增益，直接隐藏。
 * 窗口未知（未就绪 / 后端没下发）默认显示——内置模型都够大，不能误藏。
 */
export function supportsMaxContext(modelWindow?: number): boolean {
  if (typeof modelWindow !== "number" || !Number.isFinite(modelWindow) || modelWindow <= 0) {
    return true;
  }
  return modelWindow > STANDARD_WINDOW;
}
