/**
 * 模型供应商 v2 适配器：多档并存 + 只有一档启用。
 *
 * 后端契约（与 web/routers/config.py 的 v2 接口对齐）：
 * - GET  /api/providers            → { active, providers: [...] }，只回「配好的」档；
 * - POST /api/providers            → 新增 / 更新一档；不完整时 400 + 中文原因（不落库）；
 * - DELETE /api/providers/{id}     → 删一档；
 * - POST /api/providers/{id}/activate → 切换当前启用；
 * - POST /api/provider/fetch-models → 拿地址 + 密钥去问上游要模型清单（只读探测）。
 *
 * 这里只做「请求 + 字段改名」，不缓存、不猜状态：界面拿到的就是后端的事实。
 */

import type { ApiProtocol, ModelEntry, Provider } from "./ModelSettingsTemplate";

export type FetchModelsInput = { baseUrl: string; api: ApiProtocol; apiKey?: string };

export type SaveProviderInput = {
  /** 打开卡片时的整本配置版本；对不上后端会 409（期间被别处改过） */
  revision?: number;
  id?: string;
  name: string;
  kind: Provider["kind"];
  api: ApiProtocol;
  baseUrl: string;
  apiKey?: string;
  models: ModelEntry[];
  roles: Record<string, string>;
};

type ProviderDto = {
  id: string;
  name: string;
  kind: Provider["kind"];
  api: ApiProtocol;
  base_url: string;
  has_key: boolean;
  api_key_env?: string;
  key_source?: string;
  models: {
    id: string;
    label: string;
    context_window: number;
    max_output: number;
    inputs: string[];
    reasoning_levels?: string[];
    reasoning_values?: Record<string, string>;
  }[];
  roles: Record<string, string>;
  is_active: boolean;
};

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body = (await res.json().catch(() => null)) as { detail?: string } | null;
  if (!res.ok) {
    // 状态码挂到错误上：界面据此区分"别处改过（409）"和普通保存失败
    const err = new Error(body?.detail || `请求失败（${res.status}）`) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return body as T;
}

function toProvider(dto: ProviderDto): ProviderListItem {
  return {
    id: dto.id,
    name: dto.name,
    kind: dto.kind,
    api: dto.api,
    baseURL: dto.base_url,
    keyConfigured: dto.has_key,
    keySource: dto.key_source ?? "missing",
    models: dto.models.map((m) => ({
      id: m.id,
      label: m.label || m.id,
      contextWindow: m.context_window,
      maxOutput: m.max_output,
      inputs: (m.inputs ?? ["text"]) as ModelEntry["inputs"],
      reasoningLevels: m.reasoning_levels ?? [],
      reasoningValues: m.reasoning_values ?? {},
    })),
    roles: dto.roles ?? {},
    isActive: dto.is_active,
  };
}

export type ProviderListItem = Provider & {
  isActive: boolean;
  roles: Record<string, string>;
  /** 密钥从哪来（store/env/dotenv/missing），界面用来显示一句话提示 */
  keySource?: string;
};

/** 列出现存的所有「配好的」供应商，以及当前启用的是哪一个。 */
export async function listProviders(): Promise<{
  active: string;
  revision: number;
  readOnly: boolean;
  providers: ProviderListItem[];
}> {
  const dto = await call<{
    active: string;
    revision?: number;
    read_only?: boolean;
    providers: ProviderDto[];
  }>("/api/providers");
  return {
    active: dto.active,
    revision: dto.revision ?? 0,
    readOnly: Boolean(dto.read_only),
    providers: (dto.providers ?? []).map(toProvider),
  };
}

/** 新增 / 更新一档；后端校验不过会抛错（不落库），错误信息直接给用户看。 */
export async function saveProvider(input: SaveProviderInput): Promise<void> {
  await call("/api/providers", {
    method: "POST",
    body: JSON.stringify({
      revision: input.revision,
      id: input.id,
      name: input.name,
      kind: input.kind,
      api: input.api,
      base_url: input.baseUrl,
      api_key: input.apiKey,
      models: input.models.map((m) => ({
        id: m.id,
        label: m.label,
        context_window: m.contextWindow,
        max_output: m.maxOutput,
        inputs: m.inputs,
        reasoning_levels: m.reasoningLevels ?? [],
        reasoning_values: m.reasoningValues ?? {},
      })),
      roles: input.roles,
    }),
  });
}

export async function deleteProvider(id: string, revision?: number): Promise<void> {
  const qs = revision === undefined ? "" : `?revision=${revision}`;
  await call(`/api/providers/${encodeURIComponent(id)}${qs}`, { method: "DELETE" });
}

export async function activateProvider(id: string, revision?: number): Promise<void> {
  const qs = revision === undefined ? "" : `?revision=${revision}`;
  await call(`/api/providers/${encodeURIComponent(id)}/activate${qs}`, { method: "POST" });
}

/** 拿地址 + 密钥去问上游要模型清单；只探测，不保存。 */
export async function fetchModels(input: FetchModelsInput): Promise<ModelEntry[]> {
  // 后端这个接口是老形状：models 是字符串数组，能力在同序的 details 里。
  const dto = await call<{
    models: (string | { id: string; label?: string; context_window?: number; max_output?: number; inputs?: string[] })[];
    details?: { context_window?: number; supports_vision?: boolean }[];
  }>("/api/provider/fetch-models", {
    method: "POST",
    body: JSON.stringify({ base_url: input.baseUrl, api: input.api, api_key: input.apiKey }),
  });
  return (dto.models ?? []).map((raw, index) => {
    const detail = dto.details?.[index];
    if (typeof raw === "string") {
      return {
        id: raw,
        label: raw,
        contextWindow: detail?.context_window ?? 0,
        maxOutput: 0,
        inputs: (detail?.supports_vision ? ["text", "image"] : ["text"]) as ModelEntry["inputs"],
      };
    }
    return {
      id: raw.id,
      label: raw.label || raw.id,
      contextWindow: raw.context_window ?? detail?.context_window ?? 0,
      maxOutput: raw.max_output ?? 0,
      inputs: (raw.inputs ?? (detail?.supports_vision ? ["text", "image"] : ["text"])) as ModelEntry["inputs"],
    };
  });
}