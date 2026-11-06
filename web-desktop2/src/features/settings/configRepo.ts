/**
 * AI 钥匙 + 大脑模型适配器（第一版只支持阿里云「千问」/ DashScope）。
 *
 * 钥匙保存在后端：后端用它调用聊天 / 批改 / 看图模型，花费都从这把钥匙扣。
 * - GET  /api/config：只回状态（配没配、地址、当前大脑模型、思考档、最大上下文档位），**不回传明文 key**；
 * - POST /api/config：可只交钥匙、只换大脑（model + reasoning_effort）、只切上下文档位（context_mode），后端整体重建客户端；
 * - GET  /api/models：可当大脑的型号清单（含各型号思考档 / 联网 / 上下文窗口），菜单拉它、不写死。
 *
 * 走相对路径，由 Vite dev 代理（或桌面 sidecar）转到 Python 后端。
 */

export type AgentConfig = {
  /** 旧字段：默认档恒为 "dashscope"，自定义档为 "custom"（与后端 GET /api/config 对齐） */
  provider: "dashscope" | "custom";
  /** 模型来源：default=内置千问（地址内置、只填 Key）；custom=自带 BaseURL / Key / 模型清单 */
  source: ModelSource;
  hasKey: boolean;
  /** 默认千问档有没有可用 Key（自定义档下 hasKey 是自定义 Key，不能拿来判断） */
  defaultHasKey: boolean;
  baseUrl: string;
  model: string;
  /** agent 主脑思考档 low/medium/high；后端缺省回 medium */
  reasoningEffort: string;
  /** 最大上下文档位 standard/max；后端缺省回 standard */
  contextMode: string;
  /** 说话风格 natural/witty/calm；后端缺省回 natural */
  speakingStyle: SpeakingStyle;
  /** 当前装配是否真有看图能力（视觉子模型非空）；纯文本自定义端点为 false */
  supportsVision: boolean;
  /** 三能力槽（对话 / 看图 / 批改）的绑定与生效结果：槽位卡与选择器的事实源 */
  roles: AgentRoles;
  /** 本次保存发生了回退的槽位（黄条只报这一次；GET 恒为空数组） */
  roleFallbacks: string[];
  /** 自定义档详情（默认档为空结构）；只回有没有 Key，不回明文 */
  custom: CustomProviderInfo;
};

/** 模型来源两档（与后端 source 对齐） */
export type ModelSource = "default" | "custom";

/** 单个能力槽：bound=用户显式指定（null=自动适配），effective=解析后真正生效的型号 */
export type RoleBinding = {
  bound: string | null;
  effective: string | null;
};

/** 看图槽多一个 available：这台上到底有没有看图能力（纯文本端点为 false） */
export type VisionRoleBinding = RoleBinding & { available: boolean };

/** 三能力槽回显（与后端 _roles_payload 对齐）；chat 的绑定即主脑选择本身 */
export type AgentRoles = {
  chat: RoleBinding;
  vision: VisionRoleBinding;
  grade: RoleBinding;
};

/** 逐能力指定要提交的补丁（与后端 role_models 对齐；null = 回到自动适配） */
export type RoleModels = {
  vision: string | null;
  grade: string | null;
};

/** 自定义 OpenAI 兼容供应商的回显信息（字段由后端 GET /api/config 的 custom 段映射） */
export type CustomProviderInfo = {
  baseUrl: string;
  hasKey: boolean;
  models: string[];
  selectedModel: string;
  /** 每个已存模型的能力（与 models 同序）；默认档为空 */
  details: ModelCapability[];
};

/** 说话风格三档（与后端 normalize_speaking_style 对齐） */
export type SpeakingStyle = "natural" | "witty" | "calm";

/** 可当大脑的型号（字段名与后端 GET /api/models 对齐，snake_case 原样保留） */
export type BrainModel = {
  id: string;
  label: string;
  /** 模型名下方一行灰字人话描述（能力 / 快慢 / 能否联网）；后端可能不下发，为空就不渲染 */
  desc: string;
  /** 支持的思考档；空数组表示该型号不支持思考（前端把「推理强度」整行置灰） */
  reasoning_levels: string[];
  web_search: boolean;
  context_window: number;
  /** 后端按官方能力判定的品牌（openai/anthropic/google/deepseek/...）；自定义档才有，内置可空 */
  brand: string;
  /** 能否看图 / 拍照认字；缺字段按 false 保守处理（前端据此禁用图片入口） */
  supports_vision: boolean;
};

export type BrainModelsResponse = {
  default: string;
  models: BrainModel[];
  /** 「更大上下文」档位是否适用：只有内置档适用，自定义档的窗口由用户自己填 */
  context_mode_applies?: boolean;
  /** 上次选的模型已经不在清单里（下架 / 被删），前端要提示并跟着切 */
  selected_missing?: boolean;
  replaced_from?: string;
};

function normalize(d: Record<string, unknown>): AgentConfig {
  // 来源只认两档，脏值 / 缺字段一律回默认档（内置千问）。
  const source: ModelSource = d.source === "custom" ? "custom" : "default";
  const c = (d.custom ?? {}) as Record<string, unknown>;
  return {
    provider: source === "custom" ? "custom" : "dashscope",
    source,
    hasKey: Boolean(d.has_key),
    defaultHasKey: d.default_has_key === true,
    baseUrl: String(d.base_url ?? ""),
    model: String(d.model ?? ""),
    reasoningEffort: String(d.reasoning_effort ?? "medium"),
    contextMode: String(d.context_mode ?? "standard"),
    speakingStyle: normalizeSpeakingStyle(d.speaking_style),
    // 非布尔 true 不认（保守当作看不了图，由前端禁用图片入口）。
    supportsVision: d.supports_vision === true,
    roles: normalizeRoles(d.roles),
    roleFallbacks: Array.isArray(d.role_fallbacks)
      ? d.role_fallbacks.map((v) => String(v))
      : [],
    custom: {
      baseUrl: String(c.base_url ?? ""),
      hasKey: Boolean(c.has_key),
      models: Array.isArray(c.models) ? c.models.map((v) => String(v)) : [],
      selectedModel: String(c.selected_model ?? ""),
      details: Array.isArray(c.details)
        ? (c.details as Record<string, unknown>[]).map(normalizeCapability)
        : [],
    },
  };
}

/** 三能力槽回显：结构怪 / 缺字段一律按"未绑定 + 未知生效"兜，绝不抛给界面。 */
function normalizeRoles(raw: unknown): AgentRoles {
  const r = (raw ?? {}) as Record<string, unknown>;
  const chat = (r.chat ?? {}) as Record<string, unknown>;
  const vision = (r.vision ?? {}) as Record<string, unknown>;
  const grade = (r.grade ?? {}) as Record<string, unknown>;
  const optStr = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
  return {
    chat: { bound: optStr(chat.bound), effective: optStr(chat.effective) },
    vision: {
      bound: optStr(vision.bound),
      effective: optStr(vision.effective),
      available: vision.available === true,
    },
    grade: { bound: optStr(grade.bound), effective: optStr(grade.effective) },
  };
}

/** 说话风格只认三档，任何脏值（含后端缺字段）一律回自然档。 */
function normalizeSpeakingStyle(raw: unknown): SpeakingStyle {
  return raw === "natural" || raw === "witty" || raw === "calm" ? raw : "natural";
}

function normalizeModel(x: Record<string, unknown>): BrainModel {
  return {
    id: String(x.id ?? ""),
    label: String(x.label ?? x.id ?? ""),
    desc: String(x.desc ?? ""),
    reasoning_levels: Array.isArray(x.reasoning_levels)
      ? x.reasoning_levels.map((v) => String(v))
      : [],
    // 缺字段按"不能联网"保守处理，绝不假装具备能力
    web_search: x.web_search === true,
    context_window: Number(x.context_window) || 0,
    brand: typeof x.brand === "string" ? x.brand : "",
    // 非布尔 true 不认（保守当作看不了图，由前端禁用图片入口）
    supports_vision: x.supports_vision === true,
  };
}

/** 错误体里抠人话 detail；非 JSON 就退回 HTTP 状态文案 */
async function readErrorDetail(res: Response): Promise<string> {
  let d: Record<string, unknown> = {};
  try {
    d = (await res.json()) as Record<string, unknown>;
  } catch {
    /* 非 JSON 错误体 */
  }
  const detail = d.detail;
  return typeof detail === "string" ? detail : "操作没成功，请稍后重试。";
}

/** 读当前钥匙 / 大脑状态。后端不在时抛错，由调用方决定怎么提示。 */
export async function getAgentConfig(signal?: AbortSignal): Promise<AgentConfig> {
  const res = await fetch("/api/config", { signal });
  if (!res.ok) throw new Error("暂时读不到配置，请稍后重试。");
  return normalize((await res.json()) as Record<string, unknown>);
}

/** 可当大脑的型号清单。型号以后端为准，前端不写死。 */
export async function getBrainModels(signal?: AbortSignal): Promise<BrainModelsResponse> {
  const res = await fetch("/api/models", { signal });
  if (!res.ok) throw new Error("暂时拉不到模型列表，请稍后重试。");
  const d = (await res.json()) as Record<string, unknown>;
  return {
    default: String(d.default ?? ""),
    models: Array.isArray(d.models)
      ? d.models.map((m) => normalizeModel(m as Record<string, unknown>))
      : [],
    context_mode_applies: d.context_mode_applies !== false,
    selected_missing: d.selected_missing === true,
    replaced_from: String(d.replaced_from ?? ""),
  };
}

/**
 * 这台上真在跑的三个模型名（后端 GET /api/tools 的 models 段）。
 *
 * 大脑（chat）用户在设置里可选；批改（grade）/ 看图（vision）经「高级」逐能力指定、
 * 未指定走自动适配（2026-10-13 起）——设置页把它俩显示出来，是为了让人知道
 * "批改现在用的哪个"，与 /api/config 的 roles.effective 应一致。缺字段一律回空串。
 */
export type RuntimeModels = {
  /** agent 主脑（= GET /api/config 的 model，两处应一致） */
  chat: string;
  /** 批改模型（= roles.grade.effective） */
  grade: string;
  /** 看图模型（= roles.vision.effective）；空串 = 这台没有看图能力（纯文本端点） */
  vision: string;
};

/** 读真正在跑的模型名。后端不在 / 结构怪都回空串，绝不抛给界面打断渲染。 */
export async function getRuntimeModels(signal?: AbortSignal): Promise<RuntimeModels> {
  const empty: RuntimeModels = { chat: "", grade: "", vision: "" };
  try {
    const res = await fetch("/api/tools", { signal });
    if (!res.ok) return empty;
    const d = (await res.json()) as Record<string, unknown>;
    const m = (d.models ?? {}) as Record<string, unknown>;
    return {
      chat: String(m.chat ?? ""),
      grade: String(m.grade ?? ""),
      vision: String(m.vision ?? ""),
    };
  } catch {
    return empty;
  }
}

/** 保存钥匙（可选自定义 base_url，第一版一般留空、用默认千问地址）。 */
export async function saveAgentKey(apiKey: string, baseUrl?: string): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey, base_url: baseUrl ?? null }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/**
 * 保存「默认」来源档的千问 Key：地址已内置，只交 Key；后端据此切回内置千问并整体重建。
 * 只发 provider 段（source=default + default_api_key），不带模型 / 档位 / 自定义信息。
 * 填了千问 Key 即等于选回默认档（没有单独的"恢复内置"按钮）。
 */
export async function saveDefaultProviderKey(apiKey: string): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: { source: "default", default_api_key: apiKey },
    }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/** 单个模型的能力（后端 capabilities_payload）：自定义档选择器显隐思考档/上下文/看图的唯一事实源 */
export type ModelCapability = {
  id: string;
  brand: string;
  contextWindow: number;
  reasoningLevels: string[];
  supportsVision: boolean;
  canBeMain: boolean;
};

/** 一键拉取上游模型的结果：id 清单 + 与之一一对应的能力 */
export type FetchModelsResult = {
  models: string[];
  details: ModelCapability[];
};

function normalizeCapability(d: Record<string, unknown>): ModelCapability {
  return {
    id: String(d.id ?? ""),
    brand: String(d.brand ?? "generic"),
    contextWindow: Number(d.context_window ?? 128000) || 128000,
    reasoningLevels: Array.isArray(d.reasoning_levels)
      ? d.reasoning_levels.map((v) => String(v))
      : [],
    supportsVision: d.supports_vision === true,
    canBeMain: d.can_be_main !== false,
  };
}

/**
 * 一键拉取自定义端点的上游模型（只读探测、不保存）：用"待保存"的地址 / Key 请求，
 * 顺带验证通不通。失败时后端给人话 detail，这里原样抛出。
 */
export async function fetchProviderModels(
  baseUrl: string,
  apiKey: string,
): Promise<FetchModelsResult> {
  const res = await fetch("/api/provider/fetch-models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_url: baseUrl, api_key: apiKey }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  const data = (await res.json()) as Record<string, unknown>;
  const details = Array.isArray(data.details)
    ? (data.details as Record<string, unknown>[]).map(normalizeCapability)
    : [];
  return {
    models: Array.isArray(data.models) ? data.models.map((v) => String(v)) : [],
    details,
  };
}

/**
 * 切回默认千问档（不带 Key）：后端沿用已保存的千问 Key，没有就回落到 .env 那把。
 * 解决了"想切回去却被迫把 Key 再抄一遍"的问题。
 */
export async function switchToDefaultProvider(): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: { source: "default" } }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/** 保存自定义档要提交的内容（地址 / Key / 勾选进选择器的模型 / 对话首选 / 能力绑定） */
export type CustomProviderInput = {
  baseUrl: string;
  /** Key：填了就更新；null 表示沿用已保存的 Key（前端不回传明文，回显后只改模型时用） */
  apiKey: string | null;
  models: string[];
  selectedModel?: string | null;
  /** 逐能力绑定（两槽都发；null = 回到自动适配）；后端做引用修复并回传回退槽位 */
  roleModels?: RoleModels;
};

/**
 * 保存「自定义」来源档：地址、Key、勾选模型、首选、能力绑定一次性提交，后端落盘并整体重建。
 * 只发 provider custom 段；api_key 为 null 表示这次不改 Key；selected_model 缺省取清单第一个。
 */
export async function saveCustomProvider(input: CustomProviderInput): Promise<AgentConfig> {
  const provider: Record<string, unknown> = {
    source: "custom",
    custom: {
      base_url: input.baseUrl,
      api_key: input.apiKey,
      models: input.models,
      selected_model: input.selectedModel ?? null,
    },
  };
  if (input.roleModels) {
    provider.role_models = {
      vision: input.roleModels.vision,
      grade: input.roleModels.grade,
    };
  }
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider }),
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/**
 * 只保存逐能力绑定（两槽一起发，null = 回自动适配），不动地址 / Key / 清单 / 主脑。
 * 默认档与自定义档都走这里（source 随当前档传）；agent 忙碌时后端 409，界面乐观回滚。
 */
export async function saveRoleModels(
  source: ModelSource,
  roleModels: RoleModels,
  signal?: AbortSignal,
): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      provider: {
        source,
        role_models: { vision: roleModels.vision, grade: roleModels.grade },
      },
    }),
    signal,
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/**
 * 换大脑：只发 model 与 reasoning_effort，绝不带钥匙。
 * 后端按"换后型号"校验档位：非法型号 / 档位 400、agent 忙 409；
 * 错误文案抛给界面做乐观回滚与提示。批改 / 看图等子模型不受影响、也不在这里出现。
 */
export async function saveBrainChoice(
  model: string,
  reasoningEffort: string,
  signal?: AbortSignal,
): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model, reasoning_effort: reasoningEffort }),
    signal,
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/**
 * 切最大上下文档位：只发 context_mode（standard/max），绝不带钥匙 / 模型。
 * 档位只改环分母与压缩阈值；agent 忙碌时后端回 409，界面据此乐观回滚并提示。
 */
export async function saveContextMode(
  mode: string,
  signal?: AbortSignal,
): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ context_mode: mode }),
    signal,
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}

/**
 * 切说话风格：只发 speaking_style（natural/witty/calm），绝不带钥匙 / 模型。
 * 后端只换系统提示词的口吻段、随 runtime 重建从下一轮生效；agent 忙碌时回 409，
 * 界面据此乐观回滚并提示。
 */
export async function saveSpeakingStyle(
  style: SpeakingStyle,
  signal?: AbortSignal,
): Promise<AgentConfig> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ speaking_style: style }),
    signal,
  });
  if (!res.ok) throw new Error(await readErrorDetail(res));
  return normalize((await res.json()) as Record<string, unknown>);
}
