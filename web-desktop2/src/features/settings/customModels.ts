/**
 * 自定义端点模型清单的纯逻辑：把后端拉回的能力清单变成"可勾选 / 可删除 / 可搜索"
 * 的选择器候选，并投影出最终要保存的模型 id 列表与对话首选。
 *
 * 规则（与锁版设计稿 model-provider-templates 状态 3 对齐）：
 * - 非对话模型（向量 / 语音 / 画图，can_be_main=false）不进清单，只计数"已自动隐藏 N 个"；
 * - 对话模型再按 supports_vision 分「能看图」「其他对话」两组；
 * - 勾选＝显示在模型选择器，取消＝隐藏（不删除）；悬停 × 才是从清单移除；
 * - 重新拉取同 id 保留用户勾选态，上游不再返回的手动项保留；
 * - 手动按名添加走最保守的纯文本对话能力（不臆造看图 / 思考档），真正下发以后端判定为准。
 *
 * 不碰网络与 React，方便单测；网络请求在 configRepo，UI 在 Settings。
 */

import type { ModelCapability } from "./configRepo";

/** 可被勾选的候选模型：能力位 + 是否显示在选择器 */
export type SelectableModel = ModelCapability & { enabled: boolean };

/** 拉回的能力清单 → 可选候选（只留能当大脑的，默认全部勾选）+ 被隐藏的非对话数量 */
export function selectableFromDetails(details: ModelCapability[]): {
  models: SelectableModel[];
  hiddenCount: number;
} {
  const models = details
    .filter((d) => d.canBeMain)
    .map((d) => ({ ...d, enabled: true }));
  return { models, hiddenCount: details.length - models.length };
}

/** 对话模型按能不能看图分两组（都保留勾选态，取消勾选的仍显示在组内、划线置灰） */
export function partitionModels(models: SelectableModel[]): {
  vision: SelectableModel[];
  chat: SelectableModel[];
} {
  return {
    vision: models.filter((m) => m.supportsVision),
    chat: models.filter((m) => !m.supportsVision),
  };
}

/**
 * 用一次新拉取合并现有清单：
 * - 顺序以 incoming 为主，同 id 沿用现有勾选态（一键拉取不复活用户手动取消的项）；
 * - 现有里有、但这次上游没返回的（通常是手动加的）追加在末尾，不丢失。
 */
export function upsertModels(
  prev: SelectableModel[],
  incoming: SelectableModel[],
): SelectableModel[] {
  const prevById = new Map(prev.map((m) => [m.id, m]));
  const merged = incoming.map((m) => {
    const old = prevById.get(m.id);
    return old ? { ...m, enabled: old.enabled } : m;
  });
  const incomingIds = new Set(incoming.map((m) => m.id));
  for (const m of prev) {
    if (!incomingIds.has(m.id)) merged.push(m);
  }
  return merged;
}

/** 改某个模型的勾选态（勾选＝进选择器），目标不存在时原样返回 */
export function setEnabled(
  models: SelectableModel[],
  id: string,
  enabled: boolean,
): SelectableModel[] {
  return models.map((m) => (m.id === id ? { ...m, enabled } : m));
}

/** 从清单移除某个模型（悬停 ×） */
export function removeModel(models: SelectableModel[], id: string): SelectableModel[] {
  return models.filter((m) => m.id !== id);
}

/** 按模型名模糊搜索（大小写不敏感）；空搜索词返回全部 */
export function filterSelectable(models: SelectableModel[], query: string): SelectableModel[] {
  const q = query.trim().toLowerCase();
  if (!q) return models;
  return models.filter((m) => m.id.toLowerCase().includes(q));
}

/** 投影出最终保存的模型 id：只保存勾选的对话模型，保持清单顺序 */
export function enabledIds(models: SelectableModel[]): string[] {
  return models.filter((m) => m.enabled).map((m) => m.id);
}

/** 对话首选：第一个勾选模型；一个都没勾返回 null（保存按钮应据此禁用） */
export function firstEnabledId(models: SelectableModel[]): string | null {
  return models.find((m) => m.enabled)?.id ?? null;
}

/**
 * 手动按模型名添加：trim 后构造一个最保守的纯文本对话候选（默认勾选、generic、
 * 128k、无思考档、不标看图）。空白名返回 null。能力以后端真正下发时的判定为权威。
 */
export function manualModel(rawName: string): SelectableModel | null {
  const id = rawName.trim();
  if (!id) return null;
  return {
    id,
    brand: "generic",
    contextWindow: 128000,
    reasoningLevels: [],
    supportsVision: false,
    canBeMain: true,
    enabled: true,
  };
}

/** 逐能力绑定（与后端 provider.role_models 对齐；null = 自动适配） */
export type RoleModels = {
  vision: string | null;
  grade: string | null;
};

/**
 * 三能力槽的回退链解析（后端 apply/_roles_payload 的前端镜像，单一规则两处同文）：
 * - chat：选中主脑（没选则第一个勾选模型）；
 * - vision：显式绑定（必须在勾选清单内，否则视为失效）→ 清单里第一个能看图的 → null（不可用）；
 * - grade：显式绑定（同样核对）→ 跟对话同款。
 *
 * 只用于界面的"删除预告 / 即时回显"；真正生效永远以后端解析为准——这里算错不会
 * 改错模型，最多是预告文案不准，所以可以放心在前端重算。
 */
export function resolveRoles(
  models: SelectableModel[],
  roleModels: RoleModels,
  selectedModel: string | null,
): { chat: string | null; vision: string | null; grade: string | null } {
  const enabledIdsSet = new Set(models.filter((m) => m.enabled).map((m) => m.id));
  const chat = selectedModel && enabledIdsSet.has(selectedModel)
    ? selectedModel
    : (models.find((m) => m.enabled)?.id ?? null);
  const visionBinding = roleModels.vision;
  const vision =
    visionBinding && enabledIdsSet.has(visionBinding)
      ? visionBinding
      : (models.find((m) => m.enabled && m.supportsVision)?.id ?? null);
  const gradeBinding = roleModels.grade;
  const grade =
    gradeBinding && enabledIdsSet.has(gradeBinding) ? gradeBinding : chat;
  return { chat, vision, grade };
}

/** 哪些槽位当前绑定在这个模型上（删除前的确认预告用） */
export function rolesBoundTo(id: string, roleModels: RoleModels): string[] {
  const bound: string[] = [];
  if (roleModels.vision === id) bound.push("看图");
  if (roleModels.grade === id) bound.push("批改");
  return bound;
}
