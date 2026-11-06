/**
 * 大脑选择：把"启动时该选哪个大脑、要不要同步给后端"的决策做成**纯函数**，
 * React 侧只负责拉数据 / 发请求 / 存缓存（见 useBrain.ts）。
 *
 * 四条不说谎的规矩：
 * 1. 型号清单一律以后端 `GET /api/models` 为准，前端不写死；
 * 2. **后端 `GET /api/config` 是"当前用哪个脑"的唯一事实源**：启动一律采用后端型号 / 档位，
 *    浏览器缓存**只用于后端返回前的即时显示**，不许反过来改选、更不许回推（BUG-02：降级模型
 *    一旦进缓存就被反复回推、覆盖用户所选）；
 * 3. 本地缓存的型号若已下架 / 档位非法，回退默认型号 + 中档，绝不带着脏选择去请求；
 * 4. 是否需要 POST 同步，只由"**后端**型号 / 档位本身非法、我们纠了偏"决定——缓存与后端不一致
 *    不是同步理由（那正是 BUG-02 的成因）。
 */

import { readJSON, writeJSON } from "../../adapters/localStore";
import type { BrainModel, BrainModelsResponse } from "./configRepo";

export type Effort = "off" | "low" | "medium" | "high" | "xhigh" | "max";
export type BrainChoice = { model: string; effort: Effort };

export const BRAIN_STORAGE_KEY = "glyph.brain";
export const DEFAULT_EFFORT: Effort = "medium";

const EFFORT_LABEL: Record<Effort, string> = {
  off: "关",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "极高",
  max: "最高",
};

/** 档位的中文标签（胶囊 / 浮层用）；未知档位返回空串而不是乱显英文 */
/**
 * 框外那颗「思考强度」chip 上显示的短标签（一个字：低 / 中 / 高）。
 * effortLabel 给的是完整叫法，chip 上放不下；脏值返回空串，调用方据此整颗不渲染。
 */
export function effortShortLabel(effort: string | null | undefined): string {
  if (!effort) return "";
  return EFFORT_LABEL[effort as Effort] ?? "";
}

export function effortLabel(effort: string | null | undefined): string {
  if (!effort) return "";
  return EFFORT_LABEL[effort as Effort] ?? "";
}

/** 某型号在"想要某档"时实际可用的档；型号不支持思考（levels 空）返回 null */
export function effortForModel(levels: string[], wanted?: string | null): Effort | null {
  if (levels.length === 0) return null;
  if (wanted && levels.includes(wanted)) return wanted as Effort;
  if (levels.includes(DEFAULT_EFFORT)) return DEFAULT_EFFORT;
  return levels[0] as Effort; // levels 来自后端，取值应为 low/medium/high 子集
}

export type InitialInput = {
  catalog: BrainModelsResponse;
  /** 后端此刻的型号 / 档位（可能非法：型号下架、档位脏值），所以 effort 用 string 而非 Effort */
  backend: { model: string; effort: string };
};
export type InitialDecision = { choice: BrainChoice; needsSync: boolean };

/**
 * 启动时决定当前大脑，以及是否要把它同步给后端（BUG-02 修正版）。
 *
 * **以后端为唯一事实源**：型号合法就用后端给的，绝不被浏览器缓存改选；只有后端型号不在清单
 * 里（下架 / 脏值）或档位对该型号非法时才纠偏，此时 `needsSync=true`，由调用方把纠偏结果回推后端。
 */
export function resolveInitialChoice({ catalog, backend }: InitialInput): InitialDecision {
  const byId = new Map<string, BrainModel>(catalog.models.map((m) => [m.id, m]));
  const fallbackModel = byId.has(catalog.default)
    ? catalog.default
    : (catalog.models[0]?.id ?? backend.model);

  // 后端型号合法 → 直接用；非法（下架 / 脏值）→ 用清单默认型号纠偏。
  const backendModelValid = byId.has(backend.model);
  const model = backendModelValid ? backend.model : fallbackModel;
  // 后端型号合法时尊重它的档位（非法档位由 effortForModel 落到可用档）；型号非法时按中档起。
  const effort =
    effortForModel(
      byId.get(model)?.reasoning_levels ?? [],
      backendModelValid ? backend.effort : DEFAULT_EFFORT,
    ) ?? DEFAULT_EFFORT;

  const choice: BrainChoice = { model, effort };
  // 只有"后端本身非法、我们纠了偏"才需要同步；缓存与后端不一致**不是**同步理由。
  const needsSync = choice.model !== backend.model || choice.effort !== backend.effort;
  return { choice, needsSync };
}

/** 读本地缓存的选择；坏数据 / 没有都当 null（localStore 已做 JSON 与隐私模式兜底） */
export function readCachedChoice(): BrainChoice | null {
  const c = readJSON<unknown>(BRAIN_STORAGE_KEY, null);
  if (c && typeof c === "object" && typeof (c as { model?: unknown }).model === "string") {
    const o = c as { model: string; effort?: unknown };
    return {
      model: o.model,
      effort: typeof o.effort === "string" ? (o.effort as Effort) : DEFAULT_EFFORT,
    };
  }
  return null;
}

export function writeCachedChoice(choice: BrainChoice): void {
  writeJSON(BRAIN_STORAGE_KEY, choice);
}
