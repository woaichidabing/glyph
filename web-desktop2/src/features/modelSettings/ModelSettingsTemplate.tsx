import { useState, useRef, useEffect } from "react";
import "./modelSettings.css";

/**
 * 模型设置页模板（结构照 DeepSeek Harness 的模型页）。
 *
 * 界面只有三件事：列供应商 → 点开改一个 → 保存。数据全部由 props 注入，
 * 组件不拼请求、不读全局 store；接后端时把 props 换成 configRepo 的读写即可。
 *
 * 两处与 DSH 的差异：
 * 1. 没有账号体系 → 不做「DeepSeek 账号」那一行，状态点只有「已配置 / 缺失」两态；
 * 2. 每档多一个「高级设置」→ 这一档的模型分别去干哪件事（批改 / 看图 / 润色 / 压缩 / 兜底）。
 */

export type InputKind = "text" | "image";

export type ModelEntry = {
  id: string;
  label: string;
  contextWindow: number;
  maxOutput: number;
  inputs: InputKind[];
  /** 这个模型支持的思考档（low/medium/high/xhigh/max 的子集）；空 = 不支持思考 */
  reasoningLevels?: string[];
  /** 档位 → 发给厂商的参数值（不填就同档位名）；照 DSH 那张表多出来的一列 */
  reasoningValues?: Record<string, string>;
};

/** 容量格的错误文案（照 DSH 原文）；空串 = 没问题 */
const CAP_WINDOW_ERROR = "上下文窗口必须是正数，例如 131072、256K 或 1M。";
const CAP_OUTPUT_ERROR = "最大输出 token 数必须是正数，例如 8192、64K 或 1M。";

function capacityError(raw: string | undefined, message: string): string {
  if (raw === undefined) return "";
  const text = raw.trim();
  if (!text) return ""; // 留空 = 用兜底，不算错
  return parseCapacity(text) > 0 ? "" : message;
}

/** 五档词表（与后端 REASONING_LEVELS_ALL 同序）；默认只有前三个 */
export const EFFORT_LEVELS: { id: string; label: string }[] = [
  { id: "off", label: "关闭思考" },
  { id: "low", label: "低" },
  { id: "medium", label: "中" },
  { id: "high", label: "高" },
  { id: "xhigh", label: "极高" },
  { id: "max", label: "最高" },
];

/**
 * 打开推理等级时默认勾哪几档：**照 DSH 的 INITIAL_LEVELS**（off / low / medium / high）。
 *
 * 以前默认给了六档（含极高 / 最高），可大多数第三方端点只认到 high，
 * 结果用户在对话页能拖到极高、一用就 400（2026-10-28 真机踩到）。
 * 要极高 / 最高，自己在这张表里加行。
 */
const DEFAULT_EFFORTS = ["off", "low", "medium", "high"];

/** token 数按 1M / 32K 显示（DSH 的写法）；不是整倍就原样给数字 */
function formatCapacity(value: number): string {
  if (!value || value <= 0) return "";
  if (value % 1048576 === 0) return value / 1048576 + "M";
  if (value % 1024 === 0) return value / 1024 + "K";
  return String(value);
}

/** 反过来认 1M / 32K / 131072 三种写法；认不出给 0（=使用提供商默认值） */
function parseCapacity(text: string): number {
  const value = text.trim().toUpperCase().replace(/,/g, "");
  const matched = /^(\d+(?:\.\d+)?)\s*([KM])?$/.exec(value);
  if (!matched) return 0;
  const num = Number(matched[1]);
  const unit = matched[2];
  if (unit === "K") return Math.round(num * 1024);
  if (unit === "M") return Math.round(num * 1048576);
  return Math.round(num);
}

export type ApiProtocol = "openai-chat" | "openai-responses" | "anthropic-messages";

export type Provider = {
  id: string;
  name: string;
  /** builtin = 内置档（地址写死），custom = 用户自己填的端点 */
  kind: "builtin" | "custom";
  baseURL: string;
  api: ApiProtocol;
  models: ModelEntry[];
  keyConfigured: boolean;
  /** 密钥从哪来（store=本地凭据文件 / env=环境变量 / dotenv=.env / missing=没有） */
  keySource?: string;
  /** 环境里检测到的变量名，没有就不显示 */
  envHint?: string;
  isDefault?: boolean;
  /** 模型目录是用户自己改过的（显示「已自定义模型目录」） */
  modelsCustomized?: boolean;
  /** 这一档自己的角色分工：角色 → 本档的模型 id；缺省或空串 = 同主模型 */
  roles?: Record<string, string>;
};

export type RoleId = "grade" | "vision" | "polish" | "compact" | "fallback";

/** null = 同主模型（看图是「自动」、兜底是「不启用」） */
export type RoleChoice = { providerId: string; modelId: string } | null;

export type Roles = Record<RoleId, RoleChoice>;

/** 编辑已保存那一档时提交的草稿（照 DSH：编辑器只改草稿，点「应用」才提交）。 */
export type ProviderDraft = {
  name: string;
  baseURL: string;
  api: ApiProtocol;
  models: ModelEntry[];
  /** 角色分工：和模型一起提交，取消时一起丢弃 */
  roles: Record<string, string>;
  /** 空串 = 沿用已存凭据 */
  apiKey: string;
};

export type NewProviderDraft = {
  providerId: string;
  name: string;
  baseURL: string;
  api: ApiProtocol;
  apiKey: string;
  models: ModelEntry[];
};

type Props = {
  providers: Provider[];
  /** 当前服务端配置版本号：编辑卡开着时用来发现"别处改过了" */
  revision?: number;
  /** 问一次服务端的当前版本号（编辑卡轮询用） */
  fetchRevision?: () => Promise<number>;
  /** 发现被别处改过时，用户点「重试」：重新拉一遍配置 */
  onReload?: () => Promise<void>;
  /** 应用一档草稿：返回 null = 成功；返回字符串 = 就地显示的原因（409 冲突也走这里） */
  onApplyProvider?: (id: string, draft: ProviderDraft) => Promise<string | null>;
  /** 保存成功后要显示的一行字，例如「已保存 千问（DashScope）。」；空串不显示 */
  savedNotice?: string;
  /** 初次渲染就展开哪一档（预览 / 深链用） */
  initialOpenId?: string;
  /** 当前启用的是哪一档（null/空 = 还没有） */
  activeId?: string;
  /** 启用某一档（只在设置页里能切） */
  onActivate?: (providerId: string) => void;
  /** 部署把设置文档设成只读时显示提示并禁用所有按钮 */
  readOnly?: boolean;
  onSaveKey?: (providerId: string, apiKey: string) => void;
  onSaveProvider?: (providerId: string, patch: Partial<Provider>) => void;
  onDeleteProvider?: (providerId: string) => void;
  /** 添加表单里「获取可用模型」：还没保存的档也要能问上游 */
  onDiscoverModels?: (input: { baseURL: string; api: ApiProtocol; apiKey: string }) => Promise<ModelEntry[]>;
  /** 去上游要模型清单（只探测，不保存）：界面拿到清单后弹勾选框 */
  onDiscover?: (
    providerId: string,
    draft: { baseURL: string; apiKey: string },
  ) => Promise<ModelEntry[]>;
  onResetModels?: (providerId: string) => void;
  onAddProvider?: (draft: NewProviderDraft) => void;
  onRolesChange?: (providerId: string, role: RoleId, modelId: string) => void;
};

const API_LABEL: Record<ApiProtocol, string> = {
  "openai-chat": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  "anthropic-messages": "Anthropic Messages",
};

const ROLE_ROWS: { id: RoleId; label: string; followLabel: string }[] = [
  { id: "grade", label: "批改（评分 / 纠错 / 复核）", followLabel: "同主模型" },
  { id: "vision", label: "看图（OCR / 描述图片）", followLabel: "自动：第一个能看图的" },
  { id: "polish", label: "润色（句子升档）", followLabel: "同主模型" },
  { id: "compact", label: "压缩摘要（长会话）", followLabel: "同主模型" },
  { id: "fallback", label: "兜底（主模型连不上时）", followLabel: "不启用" },
];

function thousands(n: number): string {
  return n.toLocaleString("en-US");
}

export default function ModelSettingsTemplate(props: Props) {
  const { providers, savedNotice, readOnly, initialOpenId, activeId, onSaveKey, onDeleteProvider, onDiscover, onApplyProvider, onSaveProvider, onResetModels, onAddProvider, onRolesChange, onActivate } = props;

  const [openId, setOpenId] = useState<string | null>(props.initialOpenId ?? null);
  const [visibleKeyId, setVisibleKeyId] = useState<string | null>(null);
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({});
  /** 地址草稿：点「获取可用模型」时要用卡片里当前填的地址，不能只看已保存的 */
  const [urlDraft, setUrlDraft] = useState<Record<string, string>>({});
  /** 协议 / 显示名草稿：编辑卡里改的必须能存下去（以前只有 defaultValue，改了等于没改） */
  const [apiDraft, setApiDraft] = useState<Record<string, ApiProtocol>>({});
  const [nameDraft, setNameDraft] = useState<Record<string, string>>({});
  /** 容量格（上下文/输出）的原始输入：要判断"填坏了"，不能只看解析后的数字 */
  const [capDraft, setCapDraft] = useState<Record<string, string>>({});
  const [drafts, setDrafts] = useState<Record<string, ModelEntry[]>>({});
  const [expandedModel, setExpandedModel] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addTab, setAddTab] = useState<"third" | "custom">("third");
  const [pendingDelete, setPendingDelete] = useState<Provider | null>(null);
  const [fetching, setFetching] = useState(false);
  /** 勾选对话框：给哪一档加模型 / 候选清单 / 已勾选 / 搜索词 */
  const [pickTarget, setPickTarget] = useState<Provider | null>(null);
  const [pickList, setPickList] = useState<ModelEntry[]>([]);
  /** 保存被拒的原因（不落库的一次性提示，一改输入就清掉） */
  const [formError, setFormError] = useState<string | null>(null);

  const modelsOf = (p: Provider): ModelEntry[] => drafts[p.id] ?? p.models;

  const patchModels = (p: Provider, next: ModelEntry[]) => setDrafts({ ...drafts, [p.id]: next });

  const roleValue = (p: Provider, role: RoleId) => p.roles?.[role] ?? "";

  const setRole = (p: Provider, role: RoleId, value: string) => onRolesChange?.(p.id, role, value);

  type Issue = string | null;

  /**
   * 保存前校验：**没配好就不许存**，不存在“存了一半”的供应商。
   * 返回 null 表示可以保存，否则是一句人话的原因。
   */
  const validate = (draft: { baseURL: string; keyReady: boolean; models: ModelEntry[] }): Issue => {
    if (!draft.baseURL.trim()) return "还没填 API 地址。";
    if (!draft.keyReady) return "还没填 API 密钥。";
    if (draft.models.length === 0) return "模型清单一格都没填。";
    if (draft.models.some((m) => !m.id.trim())) return "有模型没填 ID。";
    const ids = draft.models.map((m) => m.id.trim());
    if (new Set(ids).size !== ids.length) return "模型 ID 有重复。";
    return null;
  };

  const handleFetch = async (p: Provider) => {
    if (!onDiscover) return;
    if (!(urlDraft[p.id] ?? p.baseURL).trim()) {
      setFormError("先填 API 地址，再去要模型清单。");
      return;
    }
    setFetching(true);
    setFormError(null);
    try {
      const found = await onDiscover(p.id, {
        baseURL: (urlDraft[p.id] ?? p.baseURL).trim(),
        apiKey: (keyDraft[p.id] ?? "").trim(),
      });
      if (found.length === 0) {
        setFormError("该提供商没有列出任何模型，请手动添加。");
        return;
      }
      setPickTarget(p);
      setPickList(found);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "拿不到模型清单");
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="ms">
      <h1 className="ms-title">模型</h1>
      <p className="ms-intro">填入各提供商的 API 密钥即可使用其模型。</p>
      {savedNotice ? <p className="ms-saved">{savedNotice}</p> : null}
      {readOnly ? <p className="ms-notice">当前部署的设置文档为只读。</p> : null}

      <ul className="ms-rows">
        {providers.map((p) => {
          const open = openId === p.id;
          const models = modelsOf(p);
          return (
            <li className="ms-card" key={p.id}>
              <div className="ms-head">
                <span className="ms-identity">
                  <span className="ms-name">{p.name}</span>
                  <span className="ms-tag">{p.kind === "builtin" ? "内置" : "自定义"}</span>
                </span>
                <span className={"ms-dot " + (p.keyConfigured ? "ok" : "bad")} title={p.keyConfigured ? "API 密钥已配置" : "API 密钥缺失"} />
                <span className="ms-actions">
                  {p.id === activeId ? (
                    <span className="ms-current">当前启用</span>
                  ) : (
                    <button className="ms-btn ghost sm" disabled={readOnly} onClick={() => onActivate?.(p.id)}>
                      启用
                    </button>
                  )}
                  <button className="ms-btn ghost sm" disabled={readOnly} onClick={() => setOpenId(open ? null : p.id)}>
                    编辑
                  </button>
                  <button className="ms-btn danger sm" disabled={readOnly} onClick={() => setPendingDelete(p)}>
                    删除
                  </button>
                </span>
              </div>

              {open ? (
                <ProviderEditor
                  provider={p}
                  readOnly={readOnly}
                  active={activeId === p.id}
                  onActivate={() => onActivate?.(p.id)}
                  onCancel={() => setOpenId(null)}
                  onApply={onApplyProvider}
                  onDelete={() => setPendingDelete(p)}
                  onRolesChange={onRolesChange}
                  onDiscoverModels={(draft) => onDiscover?.(p.id, draft) ?? Promise.resolve([])}
    onResetModels={() => onResetModels?.(p.id)}
    revision={props.revision}
    fetchRevision={props.fetchRevision}
    onReload={props.onReload}
                />
              ) : null}
            </li>
          );
        })}
      </ul>

      <div className="ms-legend">
        <span>
          <i className="ms-dot ok" /> 密钥已配置
        </span>
        <span>
          <i className="ms-dot bad" /> 密钥缺失
        </span>
        <span>
          <i className="ms-dot idle" /> 同主模型
        </span>
      </div>

      <button className="ms-add" disabled={readOnly} onClick={() => setAddOpen(!addOpen)}>
        + 添加模型提供商
      </button>

      {addOpen ? (
        <AddProviderForm
          readOnly={readOnly}
          busy={fetching}
          takenIds={providers.map((p) => p.id)}
          onDiscover={props.onDiscoverModels}
          onCancel={() => setAddOpen(false)}
          onSubmit={(draft) => {
            const issue = validateNewProvider(draft, providers.map((x) => x.id));
            if (issue) {
              setFormError(issue + "（没有被保存）");
              return;
            }
            setFormError(null);
            onAddProvider?.(draft);
            setAddOpen(false);
          }}
          onError={setFormError}
        />
      ) : null}

      {pickTarget ? (
        <ModelPickDialog
          models={pickList}
          existingIds={modelsOf(pickTarget).map((m) => m.id)}
          onCancel={() => setPickTarget(null)}
          onAdopt={(chosen) => {
            const keep = modelsOf(pickTarget).filter((m) => !chosen.some((c) => c.id === m.id));
            const merged = [...keep, ...chosen];
            patchModels(pickTarget, merged);
            onSaveProvider?.(pickTarget.id, { models: merged });
            setPickTarget(null);
            setFormError(null);
          }}
        />
      ) : null}

      {pendingDelete ? (
        <div className="ms-backdrop" onClick={(e) => e.target === e.currentTarget && setPendingDelete(null)}>
          <div className="ms-dialog">
            <h3>删除 {pendingDelete.name}</h3>
            <p className="ms-hint" style={{ fontSize: 13 }}>
              {pendingDelete.kind === "custom"
                ? "删除 " + pendingDelete.name + " 会移除其配置和存储的 API 密钥。"
                : "删除 " + pendingDelete.name + " 会移除其配置；其使用的凭证（如有）由其他位置管理，将会保留。"}
            </p>
            <div className="ms-dialog-actions">
              <button className="ms-btn ghost" onClick={() => setPendingDelete(null)}>
                取消
              </button>
              <button
                className="ms-btn primary"
                style={{ background: "var(--sem-red)", color: "#fff" }}
                onClick={() => {
                  onDeleteProvider?.(pendingDelete.id);
                  setPendingDelete(null);
                }}
              >
                删除
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------- 添加表单 */

/** 第三方厂商预置：选中就把名字 / 标识 / 地址填好，用户只需要粘密钥。 */
type Preset = {
  id: string;
  label: string;
  providerId: string;
  baseURL: string;
  api: ApiProtocol;
  /** 这一版还没有对应通道时置灰（照 DSH 的"目录里的项不可用就别让人点"） */
  unavailable?: string;
};

const PRESETS: Preset[] = [
  { id: "deepseek", label: "DeepSeek", providerId: "deepseek", baseURL: "https://api.deepseek.com/v1", api: "openai-chat" },
  { id: "moonshot", label: "Kimi（Moonshot）", providerId: "moonshot", baseURL: "https://api.moonshot.cn/v1", api: "openai-chat" },
  { id: "zhipu", label: "智谱 GLM", providerId: "zhipu", baseURL: "https://open.bigmodel.cn/api/paas/v4", api: "openai-chat" },
  { id: "openai", label: "OpenAI", providerId: "openai", baseURL: "https://api.openai.com/v1", api: "openai-chat" },
  { id: "anthropic", label: "Anthropic", providerId: "anthropic", baseURL: "https://api.anthropic.com", api: "anthropic-messages", unavailable: "这一版还没有 Anthropic 通道" },
];

function validateNewProvider(draft: NewProviderDraft, takenIds: string[] = []): string | null {
  if (!/^[a-z][a-z0-9-]*$/.test(draft.providerId.trim())) {
    return "Provider ID 要小写字母开头，只能有小写字母、数字和短横线。";
  }
  // 已有的 ID 直接拒：后端同 ID 会覆盖那一档（连角色分工一起清掉），
  // 而用户点的是「添加」，不该悄悄改掉别的档。
  if (takenIds.includes(draft.providerId.trim())) {
    return "这个 Provider ID 已经有一档了，换一个（想改它请去那一档点编辑）。";
  }
  if (!draft.baseURL.trim()) return "还没填 API 地址。";
  if (!draft.apiKey.trim()) return "还没填 API 密钥。";
  if (draft.models.length === 0) return "模型清单一格都没填：点「获取可用模型」或手动加一个。";
  if (draft.models.some((m) => !m.id.trim())) return "有模型没填 ID。";
  const ids = draft.models.map((m) => m.id.trim());
  if (new Set(ids).size !== ids.length) return "模型 ID 有重复。";
  return null;
}

type AddFormProps = {
  readOnly?: boolean;
  busy?: boolean;
  /** 已经配过的 providerId：目录里这些显示「已添加」并不让再选 */
  takenIds?: string[];
  onDiscover?: (input: { baseURL: string; api: ApiProtocol; apiKey: string }) => Promise<ModelEntry[]>;
  onCancel: () => void;
  onSubmit: (draft: NewProviderDraft) => void;
  onError: (message: string | null) => void;
};

/** 添加一档供应商：字段与 DeepSeek Harness 那张卡一致（含密钥、模型目录、获取可用模型）。 */
function AddProviderForm({ readOnly, busy, takenIds = [], onDiscover, onCancel, onSubmit, onError }: AddFormProps) {
  const [tab, setTab] = useState<"third" | "custom">("third");
  const [providerId, setProviderId] = useState("");
  const [name, setName] = useState("");
  const [baseURL, setBaseURL] = useState("");
  const [api, setApi] = useState<ApiProtocol>("openai-chat");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<ModelEntry[]>([]);
  /** 弹窗里也能展开某个模型改细节（窗口/输出/思考档），与已保存卡片一致 */
  const [expandedDraftModel, setExpandedDraftModel] = useState<string | null>(null);
  const [capDraft, setCapDraft] = useState<Record<string, string>>({});
  const [fetching, setFetching] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  /** 拉回来的候选清单：先弹勾选框，勾了才进目录 */
  const [pickList, setPickList] = useState<ModelEntry[] | null>(null);
  const draft = (): NewProviderDraft => ({ providerId, name, baseURL, api, apiKey, models });

  const applyPreset = (id: string) => {
    const preset = PRESETS.find((x) => x.id === id);
    if (!preset) return;
    setProviderId(preset.providerId);
    setName(preset.label);
    setBaseURL(preset.baseURL);
    setApi(preset.api);
  };

  const discover = async () => {
    if (!onDiscover) return;
    if (!baseURL.trim()) { setLocalError("先填 API 地址，再去要模型清单。"); return; }
    if (!apiKey.trim()) { setLocalError("先填 API 密钥，再去要模型清单。"); return; }
    setFetching(true);
    setLocalError(null);
    try {
      const found = await onDiscover({ baseURL: baseURL.trim(), api, apiKey: apiKey.trim() });
      if (found.length === 0) {
        setLocalError("该提供商没有列出任何模型，请手动添加。");
        return;
      }
      setPickList(found);
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : "拿不到模型清单");
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="ms-panel">
      <div className="ms-seg">
        <button aria-pressed={tab === "third"} onClick={() => setTab("third")}>
          第三方模型提供商
        </button>
        <button aria-pressed={tab === "custom"} onClick={() => setTab("custom")}>
          自定义模型 API
        </button>
      </div>

      {tab === "third" ? (
        <div className="ms-field">
          <label className="ms-label">提供商</label>
          <select className="ms-select narrow" defaultValue="" onChange={(e) => applyPreset(e.target.value)}>
            <option value="">选一家…</option>
            {PRESETS.map((preset) => {
              const taken = takenIds.includes(preset.providerId);
              return (
                <option key={preset.id} value={preset.id} disabled={taken || Boolean(preset.unavailable)}>
                  {preset.label}
                  {taken ? "（已添加）" : preset.unavailable ? `（${preset.unavailable}）` : ""}
                </option>
              );
            })}
          </select>
          <p className="ms-hint">
            {PRESETS.every((p) => takenIds.includes(p.providerId) || p.unavailable)
              ? "目录中的提供商都已添加。"
              : "选中会自动填好名称、标识和地址，你只要粘密钥。"}
          </p>
        </div>
      ) : (
        <p className="ms-hint">
          连接中转站、自部署服务或其他兼容 OpenAI / Anthropic 协议的接口，需要填地址、协议和模型。
        </p>
      )}

      <div className="ms-field">
        <label className="ms-label">Provider ID</label>
        <input
          className="ms-input"
          value={providerId}
          placeholder="acme-gateway"
          onChange={(e) => { setProviderId(e.target.value); onError(null); }}
        />
        <p className="ms-hint">以小写字母开头的标识，在请求中唯一标识该提供商，并用于派生凭据名。</p>
      </div>

      <div className="ms-field">
        <label className="ms-label">显示名称</label>
        <input
          className="ms-input"
          value={name}
          /* 照 DSH：占位符就是上面那个 ID，等于告诉用户"留空就用它" */
          placeholder={providerId.trim() || "显示名称"}
          onChange={(e) => setName(e.target.value)}
        />
        <p className="ms-hint">留空就用上面的 ID，想给它起个中文名再填。</p>
      </div>

      <div className="ms-field">
        <label className="ms-label">API 地址</label>
        <input
          className="ms-input"
          value={baseURL}
          placeholder="https://gateway.example/v1"
          onChange={(e) => setBaseURL(e.target.value)}
        />
      </div>

      <div className="ms-field">
        <label className="ms-label">API 协议</label>
        <select
          className="ms-select narrow"
          value={api}
          onChange={(e) => setApi(e.target.value as ApiProtocol)}
        >
          {(Object.keys(API_LABEL) as ApiProtocol[]).map((key) => (
            <option key={key} value={key}>
              {API_LABEL[key]}
            </option>
          ))}
        </select>
      </div>

      <div className="ms-field">
        <label className="ms-label">API 密钥</label>
        <input
          className="ms-input"
          type="password"
          autoComplete="new-password"
          value={apiKey}
          placeholder="输入 API 密钥"
          onChange={(e) => { setApiKey(e.target.value); onError(null); }}
        />
      </div>

      <div className="ms-field">
        <label className="ms-label">
          模型目录
          <button
            className="ms-btn link"
            style={{ marginLeft: "auto" }}
            disabled={readOnly || busy || fetching}
            onClick={() => void discover()}
          >
            {fetching ? "正在询问提供商…" : "获取可用模型"}
          </button>
        </label>

        {models.length === 0 ? (
          <div className="ms-empty">还没有任何模型：点「获取可用模型」从端点拉，或者手动加一个。</div>
        ) : (
          <div className="ms-models">
            {models.map((model, index) => (
              <div className="ms-mitem" key={index}>
                <div className="ms-mrow">
                  <input
                    className="ms-mcell"
                    value={model.id}
                    placeholder="模型 ID"
                    onChange={(e) =>
                      setModels(models.map((m, i) => (i === index ? { ...m, id: e.target.value } : m)))
                    }
                  />
                  <input
                    className="ms-mcell ms-mcell-name"
                    value={model.label}
                    placeholder="显示名称"
                    onChange={(e) =>
                      setModels(models.map((m, i) => (i === index ? { ...m, label: e.target.value } : m)))
                    }
                  />
                  <button
                    className="ms-icon"
                    aria-label="更多设置"
                    aria-expanded={expandedDraftModel === String(index)}
                    onClick={() =>
                      setExpandedDraftModel(
                        expandedDraftModel === String(index) ? null : String(index),
                      )
                    }
                  >
                    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4">
                      <path
                        d="M5 3l4 4-4 4"
                        style={{
                          transform: expandedDraftModel === String(index) ? "rotate(90deg)" : undefined,
                          transformOrigin: "7px 7px",
                        }}
                      />
                    </svg>
                  </button>
                  <button
                    className="ms-icon"
                    aria-label="删除这个模型"
                    onClick={() => setModels(models.filter((_, i) => i !== index))}
                  >
                    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3">
                      <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8h5.8l.6-8" />
                    </svg>
                  </button>
                </div>
                {expandedDraftModel === String(index) ? (
                  <CapFields
                    p={{ id: "__draft__", kind: "custom" } as unknown as Provider}
                    m={model}
                    models={models}
                    patchModels={(_p, next) => setModels(next)}
                    capDraft={capDraft}
                    setCapDraft={setCapDraft}
                    capKeyPrefix={"draft:" + index}
                  />
                ) : null}
              </div>
            ))}
          </div>
        )}

        <button
          className="ms-btn link"
          style={{ alignSelf: "flex-start", marginLeft: -10 }}
          disabled={readOnly}
          onClick={() =>
            setModels([...models, { id: "", label: "", contextWindow: 1048576, maxOutput: 0, inputs: ["text"] }])
          }
        >
          + 添加模型
        </button>
      </div>

      {pickList ? (
        <ModelPickDialog
          models={pickList}
          existingIds={models.map((m) => m.id)}
          onCancel={() => setPickList(null)}
          onAdopt={(chosen) => {
            setModels((cur) => {
              const have = new Set(cur.map((m) => m.id));
              return [...cur, ...chosen.filter((m) => !have.has(m.id))];
            });
            setPickList(null);
          }}
        />
      ) : null}

      {localError ? <p className="ms-error">{localError}</p> : null}

      <div className="ms-actions" style={{ justifyContent: "flex-end" }}>
        <button className="ms-btn ghost" onClick={onCancel}>
          取消
        </button>
        <button
          className="ms-btn primary"
          disabled={readOnly}
          onClick={() => {
            const d = draft();
            // 校验就地做：报错显示在按钮上方，别写到别处让人以为点了没反应
            const issue = validateNewProvider(d, takenIds);
            if (issue) { setLocalError(issue); return; }
            setLocalError(null);
            onSubmit(d);
          }}
        >
          添加
        </button>
      </div>
    </div>
  );
}
/* ---------------------------------------------------------------- 勾选对话框 */

type PickDialogProps = {
  models: ModelEntry[];
  /** 已经在目录里的 id：默认不勾，避免重复添加 */
  existingIds: string[];
  onCancel: () => void;
  onAdopt: (chosen: ModelEntry[]) => void;
};

/** 「选择要添加的模型」：拉回来的清单先给人过目，勾了才加进去（照 DSH 的流程）。 */
function ModelPickDialog({ models, existingIds, onCancel, onAdopt }: PickDialogProps) {
  const [search, setSearch] = useState("");
  const [picked, setPicked] = useState<Set<string>>(
    () => new Set(models.filter((m) => !existingIds.includes(m.id)).map((m) => m.id)),
  );

  const keyword = search.trim().toLowerCase();
  const shown = models.filter(
    (m) => !keyword || (m.label || m.id).toLowerCase().includes(keyword) || m.id.toLowerCase().includes(keyword),
  );

  return (
    <div
      className="ms-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="ms-dialog" role="dialog" aria-label="选择要添加的模型">
        <h3>选择要添加的模型</h3>
        <p className="ms-hint">以下是模型提供商的可用模型，勾选要添加的模型。</p>
        <input
          className="ms-input"
          type="search"
          placeholder="搜索模型"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="ms-dialog-list">
          {shown.map((m) => (
            <label key={m.id}>
              <input
                type="checkbox"
                checked={picked.has(m.id)}
                onChange={(e) => {
                  const next = new Set(picked);
                  if (e.target.checked) next.add(m.id);
                  else next.delete(m.id);
                  setPicked(next);
                }}
              />
              {m.label || m.id}
            </label>
          ))}
          {shown.length === 0 ? <p className="ms-hint">没有匹配的模型。</p> : null}
        </div>
        <div className="ms-dialog-actions">
          <button className="ms-btn link" onClick={() => setPicked(new Set(models.map((m) => m.id)))}>
            全选
          </button>
          <button className="ms-btn link" onClick={() => setPicked(new Set<string>())}>
            取消全选
          </button>
          <span style={{ flex: 1 }} />
          <button className="ms-btn ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className="ms-btn primary"
            onClick={() => onAdopt(models.filter((m) => picked.has(m.id)))}
          >
            添加所选
          </button>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- 编辑一档（照 DSH） */

/**
 * 一档供应商的编辑器。
 *
 * 照 DSH 的两条关键做法：
 * 1. **只改草稿**：打开时把已存的值拷一份，取消 = 丢掉副本，原数据没被碰过；
 * 2. **行用序号当身份**：改模型 ID 不会让整行重建，也就不会丢输入焦点。
 */
function ProviderEditor({
  provider,
  readOnly,
  active,
  onActivate,
  onCancel,
  onApply,
  onDelete,
  onRolesChange,
  onDiscoverModels,
  onResetModels,
  revision,
  fetchRevision,
  onReload,
}: {
  provider: Provider;
  readOnly?: boolean;
  active: boolean;
  onActivate: () => void;
  onCancel: () => void;
  onApply?: (id: string, draft: ProviderDraft) => Promise<string | null>;
  onDelete: () => void;
  onRolesChange?: (providerId: string, role: RoleId, modelId: string) => void;
  onDiscoverModels: (input: { baseURL: string; api: ApiProtocol; apiKey: string }) => Promise<ModelEntry[]>;
  onResetModels?: () => void;
  revision?: number;
  fetchRevision?: () => Promise<number>;
  onReload?: () => Promise<void>;
}) {
  const [name, setName] = useState(provider.name);
  const [baseURL, setBaseURL] = useState(provider.baseURL);
  const [api, setApi] = useState<ApiProtocol>(provider.api);
  const [models, setModels] = useState<ModelEntry[]>(provider.models);
  const [roles, setRoles] = useState<Record<string, string>>(provider.roles ?? {});
  const [keyValue, setKeyValue] = useState("");
  const [keyShown, setKeyShown] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  /** 容量格的原始输入（要判"填坏了"，不能只看解析后的数字） */
  const [capDraft, setCapDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [fetching, setFetching] = useState(false);
  const [pickList, setPickList] = useState<ModelEntry[] | null>(null);

  /** 打开这张卡时的版本号；轮询发现变化就提示"别处改过了"（照 DSH） */
  const openedRevision = useRef<number | undefined>(revision);
  const [stale, setStale] = useState(false);
  const [reloading, setReloading] = useState(false);

  useEffect(() => {
    if (!fetchRevision) return;
    let alive = true;
    const tick = async () => {
      if (busy) return;
      try {
        const now = await fetchRevision();
        if (alive && openedRevision.current !== undefined && now !== openedRevision.current) {
          setStale(true);
        }
      } catch {
        /* 问不到就先不判，下一次再试 */
      }
    };
    const timer = window.setInterval(() => void tick(), 4000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [fetchRevision, busy]);

  const retry = async () => {
    if (!onReload) return;
    setReloading(true);
    try {
      await onReload();
      setStale(false);
    } finally {
      setReloading(false);
    }
  };

  const patchModel = (index: number, next: Partial<ModelEntry>) =>
    setModels((rows) => rows.map((row, i) => (i === index ? { ...row, ...next } : row)));

  const validate = (): string | null => {
    for (const [k, v] of Object.entries(capDraft)) {
      if (!k.startsWith(provider.id + ":row:")) continue;
      if (capacityError(v, CAP_WINDOW_ERROR) || capacityError(v, CAP_OUTPUT_ERROR)) {
        return "有一行的窗口/输出填得不对（例如 abc），先改掉或留空。";
      }
    }
    if (!baseURL.trim()) return "自定义提供商需要填 API 地址。";
    if (!/^https?:\/\/\S+$/i.test(baseURL.trim())) return "API 地址要是合法的 http 或 https 链接。";
    if (models.length === 0) return "至少要有一个模型。";
    const ids = models.map((m) => m.id.trim());
    if (ids.some((id) => !id)) return "模型 ID 不能为空。";
    const dup = ids.find((id, i) => ids.indexOf(id) !== i);
    if (dup) return "模型 ID 不能重复：" + dup;
    for (let i = 0; i < models.length; i += 1) {
      const m = models[i];
      if (!m.label.trim()) return "第 " + (i + 1) + " 行：显示名称不能为空。";
      if (m.contextWindow < 0) return "第 " + (i + 1) + " 行：上下文窗口必须是正数。";
      if (m.maxOutput < 0) return "第 " + (i + 1) + " 行：最大输出 token 数必须是正数。";
      const levels = m.reasoningLevels ?? [];
      if (levels.length > 0) {
        if (!levels.some((l) => l !== "off")) return "第 " + (i + 1) + " 行：除了 Off，至少还要一档。";
        for (const l of levels) {
          if (l !== "off" && !(m.reasoningValues?.[l] ?? "").trim()) {
            return "第 " + (i + 1) + " 行：档位 " + l + " 需要填 API 参数值。";
          }
        }
      }
    }
    return null;
  };

  const apply = async () => {
    if (!onApply) return;
    const issue = validate();
    if (issue) {
      setFailure(issue);
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      const problem = await onApply(provider.id, {
        name: name.trim() || provider.id,
        baseURL: baseURL.trim(),
        api,
        models,
        roles,
        apiKey: keyValue.trim(),
      });
      if (problem) {
        setFailure(problem);
        return;
      }
      setKeyValue("");
      // 自己刚写完：把"打开时版本号"对齐到最新，否则下一次轮询会误报"别处改过"
      if (fetchRevision) {
        try { openedRevision.current = await fetchRevision(); } catch { /* 问不到就下次再说 */ }
      }
      onCancel();
    } finally {
      setBusy(false);
    }
  };

  const discover = async () => {
    setFailure(null);
    if (!baseURL.trim()) {
      setFailure("先填 API 地址，再去要模型清单。");
      return;
    }
    if (!keyValue.trim() && !provider.keyConfigured) {
      setFailure("先填 API 密钥，再去要模型清单。");
      return;
    }
    setFetching(true);
    try {
      const found = await onDiscoverModels({ baseURL: baseURL.trim(), api, apiKey: keyValue.trim() });
      if (found.length === 0) {
        setFailure("这个提供商没有列出任何模型，手动加一个吧。");
        return;
      }
      setPickList(found);
    } catch (err) {
      setFailure(err instanceof Error ? err.message : "拉取模型清单失败。");
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="ms-editor">
      {stale ? (
        <div className="ms-conflict">
          <span>这份配置在你打开卡片期间被别处改过了。关掉重开，改当前的值。</span>
          <button className="ms-btn ghost sm" disabled={reloading} onClick={() => void retry()}>
            {reloading ? "正在刷新…" : "重试"}
          </button>
        </div>
      ) : null}
      <div className="ms-editor-head">
        <span>编辑 {provider.name}</span>
        {active ? <span className="ms-tag">当前启用</span> : null}
        <div className="ms-editor-head-actions">
          {active ? null : (
            <button className="ms-btn ghost sm" disabled={readOnly} onClick={onActivate}>
              启用
            </button>
          )}
          <button className="ms-btn danger sm" disabled={readOnly} onClick={onDelete}>
            删除
          </button>
        </div>
      </div>

      <div className="ms-field">
        <label className="ms-label">显示名称</label>
        <input
          className="ms-input"
          value={name}
          placeholder={provider.id}
          onChange={(e) => setName(e.target.value)}
        />
        <p className="ms-hint">留空就用 Provider ID。</p>
      </div>

      <div className="ms-field">
        <label className="ms-label">API 地址</label>
        <input
          className="ms-input"
          value={baseURL}
          placeholder="https://gateway.example/v1"
          disabled={provider.kind === "builtin"}
          onChange={(e) => setBaseURL(e.target.value)}
        />
        <p className="ms-hint">
          {provider.kind === "builtin" ? "内置档的地址由系统维护。" : "兼容 OpenAI 的接口地址，通常以 /v1 结尾。"}
        </p>
      </div>

      <div className="ms-field">
        <label className="ms-label">API 协议</label>
        <select
          className="ms-select narrow"
          value={api}
          disabled={provider.kind === "builtin"}
          onChange={(e) => setApi(e.target.value as ApiProtocol)}
        >
          <option value="openai-chat">OpenAI Chat Completions</option>
          <option value="openai-responses">OpenAI Responses</option>
          <option value="anthropic-messages" disabled>
            Anthropic Messages（这一版还不支持）
          </option>
        </select>
      </div>

      <div className="ms-field">
        <label className="ms-label">API 密钥</label>
        <div className="ms-keyrow">
          <input
            className="ms-input"
            type={keyShown ? "text" : "password"}
            value={keyValue}
            placeholder={provider.keyConfigured ? "留空使用已保存的密钥" : "粘贴 API 密钥"}
            onChange={(e) => {
              setKeyValue(e.target.value);
              setFailure(null);
            }}
          />
          <button
            type="button"
            className="ms-btn ghost sm"
            onClick={() => setKeyShown((v) => !v)}
            aria-pressed={keyShown}
          >
            {keyShown ? "隐藏" : "显示"}
          </button>
        </div>
        <p className="ms-hint">
          {provider.keyConfigured
            ? "已保存过密钥；留空就继续用它。"
            : provider.envHint
              ? "没检测到环境变量 " + provider.envHint + "，请直接粘贴密钥。"
              : "还没有密钥。"}
        </p>
      </div>

      <div className="ms-field">
        <div className="ms-models-head">
          <label className="ms-label">模型目录</label>
          <span className="ms-models-head-actions">
            <button
              className="ms-btn link"
              disabled={readOnly || provider.kind === "builtin"}
              onClick={() => onResetModels?.()}
            >
              恢复默认模型
            </button>
            <button className="ms-btn link" disabled={readOnly || fetching} onClick={() => void discover()}>
              {fetching ? "正在询问提供商…" : "获取可用模型"}
            </button>
          </span>
        </div>
        <p className="ms-hint">
          {provider.kind === "builtin"
            ? "正在使用适配器默认模型（随版本更新，不需要手动维护）。"
            : provider.modelsCustomized
              ? "已自定义模型目录。"
              : null}
        </p>
        {models.length === 0 ? (
          <div className="ms-empty">还没有任何模型：点「获取可用模型」，或手动加一个。</div>
        ) : (
          <div className="ms-models">
            {models.map((m, index) => {
              const key = provider.id + ":row:" + index;
              const open = expanded === index;
              return (
                <div className="ms-mitem" key={index}>
                  <div className="ms-mrow">
                    <input
                      className="ms-mcell"
                      value={m.id}
                      placeholder="模型 ID"
                      onChange={(e) => patchModel(index, { id: e.target.value })}
                    />
                    <input
                      className="ms-mcell ms-mcell-name"
                      value={m.label}
                      placeholder={m.id || "显示名称"}
                      onChange={(e) => patchModel(index, { label: e.target.value })}
                    />
                    <button
                      className="ms-icon"
                      aria-label="模型选项"
                      aria-expanded={open}
                      onClick={() => setExpanded(open ? null : index)}
                    >
                      <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4">
                        <path
                          d="M5 3l4 4-4 4"
                          style={{ transform: open ? "rotate(90deg)" : undefined, transformOrigin: "7px 7px" }}
                        />
                      </svg>
                    </button>
                    <button
                      className="ms-icon"
                      aria-label="删除这个模型"
                      onClick={() => {
                        setModels((rows) => rows.filter((_, i) => i !== index));
                        setExpanded(null);
                        // 行号会前移：把这一行（含后面各行）的草稿清掉，免得串到别的模型
                        setCapDraft((d) => {
                          const next: Record<string, string> = {};
                          for (const [k, v] of Object.entries(d)) {
                            if (!k.startsWith(provider.id + ":row:")) next[k] = v;
                            else {
                              const idx = Number(k.split(":row:")[1].split(":")[0]);
                              if (idx < index) next[k] = v;
                            }
                          }
                          return next;
                        });
                      }}
                    >
                      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3">
                        <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8h5.8l.6-8" />
                      </svg>
                    </button>
                  </div>
                  {open ? (
                    <CapFields
                      p={provider}
                      m={m}
                      models={models}
                      patchModels={(_p, next) => setModels(next)}
                      capDraft={capDraft}
                      setCapDraft={setCapDraft}
                      capKeyPrefix={key}
                    />
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
        <button
          className="ms-btn link"
          style={{ alignSelf: "flex-start", marginLeft: -10 }}
          disabled={readOnly}
          onClick={() => {
            setModels((rows) => [
              ...rows,
              { id: "", label: "", contextWindow: 0, maxOutput: 0, inputs: ["text"] },
            ]);
            setExpanded(models.length);
          }}
        >
          + 添加模型
        </button>
      </div>

      {provider.roles ? (
        <div className="ms-field">
          <label className="ms-label">角色分工</label>
          <p className="ms-hint">不填就用主模型。</p>
          <div className="ms-roles">
            {ROLE_ROWS.map((row) => (
              <div className="ms-role" key={row.id}>
                <span className="ms-role-name">{row.label}</span>
                <select
                  className="ms-select"
                  value={roles[row.id] ?? ""}
                  disabled={readOnly}
                  onChange={(e) => setRoles((cur) => ({ ...cur, [row.id]: e.target.value }))}
                >
                  <option value="">同主模型</option>
                  {models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label || m.id}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {failure ? <p className="ms-error">{failure}</p> : null}

      <div className="ms-actions" style={{ justifyContent: "flex-end" }}>
        <button className="ms-btn ghost" disabled={busy} onClick={onCancel}>
          取消
        </button>
        <button className="ms-btn primary" disabled={readOnly || busy} onClick={() => void apply()}>
          {busy ? "正在应用…" : "应用"}
        </button>
      </div>

      {pickList ? (
        <ModelPickDialog
          models={pickList}
          existingIds={models.map((m) => m.id)}
          onCancel={() => setPickList(null)}
          onAdopt={(chosen) => {
            setModels((rows) => {
              const have = new Set(rows.map((r) => r.id));
              return [...rows, ...chosen.filter((c) => !have.has(c.id))];
            });
            setPickList(null);
          }}
        />
      ) : null}
    </div>
  );
}

/** 一个模型的「更多设置」：窗口 / 输出 / 输入类型 / 思考档。
 *  已保存的供应商卡片和「添加供应商」弹窗共用这一份，避免两处长得不一样。 */
function CapFields({
  p,
  m,
  models,
  patchModels,
  capDraft,
  setCapDraft,
  capKeyPrefix,
}: {
  p: Provider;
  m: ModelEntry;
  models: ModelEntry[];
  patchModels: (p: Provider, next: ModelEntry[]) => void;
  capDraft: Record<string, string>;
  setCapDraft: (next: Record<string, string>) => void;
  /** 草稿键前缀：编辑卡用序号（改 ID 不会串），缺省用 "档:模型ID" */
  capKeyPrefix?: string;
}) {
  const capKey = capKeyPrefix ?? p.id + ":" + m.id;
  return (
                                  <div className="ms-mdetail">
                                    <div className="ms-field">
                                      <label className="ms-label">上下文窗口</label>
                                      <input
                                        className={
                                          "ms-input" +
                                          (capacityError(capDraft[capKey + ":cw"], CAP_WINDOW_ERROR) ? " bad" : "")
                                        }
                                        placeholder="使用提供商默认值"
                                        value={
                                          capDraft[capKey + ":cw"] ??
                                          formatCapacity(m.contextWindow)
                                        }
                                        onChange={(e) => {
                                          const raw = e.target.value;
                                          setCapDraft({ ...capDraft, [capKey + ":cw"]: raw });
                                          patchModels(
                                            p,
                                            models.map((x) =>
                                              x.id === m.id ? { ...x, contextWindow: parseCapacity(raw) } : x,
                                            ),
                                          );
                                        }}
                                      />
                                      {capacityError(capDraft[capKey + ":cw"], CAP_WINDOW_ERROR) ? (
                                        <p className="ms-hint bad">{CAP_WINDOW_ERROR}</p>
                                      ) : null}
                                    </div>
                                    <div className="ms-field">
                                      <label className="ms-label">最大输出 token 数</label>
                                      <input
                                        className={
                                          "ms-input" +
                                          (capacityError(
                                            capDraft[capKey + ":mo"],
                                            CAP_OUTPUT_ERROR,
                                          )
                                            ? " bad"
                                            : "")
                                        }
                                        placeholder="使用提供商默认值"
                                        value={
                                          capDraft[capKey + ":mo"] ??
                                          formatCapacity(m.maxOutput)
                                        }
                                        onChange={(e) => {
                                          const raw = e.target.value;
                                          setCapDraft({ ...capDraft, [capKey + ":mo"]: raw });
                                          patchModels(
                                            p,
                                            models.map((x) =>
                                              x.id === m.id ? { ...x, maxOutput: parseCapacity(raw) } : x,
                                            ),
                                          );
                                        }}
                                      />
                                      {capacityError(capDraft[capKey + ":mo"], CAP_OUTPUT_ERROR) ? (
                                        <p className="ms-hint bad">{CAP_OUTPUT_ERROR}</p>
                                      ) : null}
                                    </div>
                                    <div className="ms-field">
                                      <label className="ms-label">输入类型</label>
                                      <div className="ms-checks">
                                        {(["text", "image"] as InputKind[]).map((kind) => (
                                          <label key={kind}>
                                            <input
                                              type="checkbox"
                                              checked={m.inputs.includes(kind)}
                                              onChange={(e) =>
                                                patchModels(
                                                  p,
                                                  models.map((x) =>
                                                    x.id === m.id
                                                      ? {
                                                          ...x,
                                                          inputs: e.target.checked
                                                            ? [...x.inputs, kind]
                                                            : x.inputs.filter((k) => k !== kind),
                                                        }
                                                      : x,
                                                  ),
                                                )
                                              }
                                            />
                                            {kind === "text" ? "文本" : "图片"}
                                          </label>
                                        ))}
                                      </div>
                                    </div>
                                    {p.kind === "custom" ? (
                                      <div className="ms-field" style={{ gridColumn: "1 / -1" }}>
                                        <label className="ms-label">
                                          推理等级
                                          <button className="ms-help" title="档位与参数值要和厂商文档一致；参数值留空时就用档位名">?</button>
                                          <span className="ms-toggle">
                                            <button
                                              type="button"
                                              role="switch"
                                              aria-checked={(m.reasoningLevels ?? []).length > 0}
                                              className={"ms-switch" + ((m.reasoningLevels ?? []).length > 0 ? " on" : "")}
                                              onClick={() =>
                                                patchModels(
                                                  p,
                                                  models.map((x) =>
                                                    x.id === m.id
                                                      ? {
                                                          ...x,
                                                          ...(x.reasoningLevels ?? []).length > 0
                                                          ? { reasoningLevels: [], reasoningValues: {} }
                                                          : {
                                                              reasoningLevels: DEFAULT_EFFORTS,
                                                              reasoningValues: Object.fromEntries(
                                                                DEFAULT_EFFORTS.filter((l) => l !== "off").map((l) => [l, l]),
                                                              ),
                                                            },
                                                        }
                                                      : x,
                                                  ),
                                                )
                                              }
                                            >
                                              <span className="ms-switch-knob" />
                                            </button>
                                          </span>
                                        </label>

                                        {(m.reasoningLevels ?? []).length > 0 ? (
                                          <div className="ms-efforts">
                                            <div className="ms-efforts-head">
                                              <span>档位</span>
                                              <span>API 参数值</span>
                                              <span />
                                            </div>
                                            {(m.reasoningLevels ?? []).map((level, index) => (
                                              <div className="ms-effort-row" key={level + ":" + index}>
                                                <select
                                                  className="ms-select"
                                                  value={level}
                                                  onChange={(e) =>
                                                    patchModels(
                                                      p,
                                                      models.map((x) =>
                                                        x.id === m.id
                                                          ? {
                                                              ...x,
                                                              reasoningLevels: (x.reasoningLevels ?? []).map((l, i) =>
                                                                i === index ? e.target.value : l,
                                                              ),
                                                            }
                                                          : x,
                                                      ),
                                                    )
                                                  }
                                                >
                                                  {EFFORT_LEVELS.map((option) => (
                                                    <option key={option.id} value={option.id}>
                                                      {option.id}
                                                    </option>
                                                  ))}
                                                </select>
                                                <input
                                                  className="ms-input"
                                                  placeholder={level === "off" ? "关闭思考" : "留空＝同档位名"}
                                                  disabled={level === "off"}
                                                  onBlur={() => {
                                                    if (level === "off") return;
                                                    const current = (m.reasoningValues ?? {})[level] ?? "";
                                                    if (current.trim()) return;
                                                    patchModels(
                                                      p,
                                                      models.map((x) =>
                                                        x.id === m.id
                                                          ? {
                                                              ...x,
                                                              reasoningValues: {
                                                                ...(x.reasoningValues ?? {}),
                                                                [level]: level,
                                                              },
                                                            }
                                                          : x,
                                                      ),
                                                    );
                                                  }}
                                                  value={level === "off" ? "" : (m.reasoningValues ?? {})[level] ?? ""}
                                                  onChange={(e) =>
                                                    patchModels(
                                                      p,
                                                      models.map((x) =>
                                                        x.id === m.id
                                                          ? {
                                                              ...x,
                                                              reasoningValues: {
                                                                ...(x.reasoningValues ?? {}),
                                                                [level]: e.target.value,
                                                              },
                                                            }
                                                          : x,
                                                      ),
                                                    )
                                                  }
                                                />
                                                <button
                                                  className="ms-icon"
                                                  aria-label="删除档位"
                                                  onClick={() =>
                                                    patchModels(
                                                      p,
                                                      models.map((x) =>
                                                        x.id === m.id
                                                          ? {
                                                              ...x,
                                                              reasoningLevels: (x.reasoningLevels ?? []).filter(
                                                                (_, i) => i !== index,
                                                              ),
                                                            }
                                                          : x,
                                                      ),
                                                    )
                                                  }
                                                >
                                                  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3">
                                                    <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8h5.8l.6-8" />
                                                  </svg>
                                                </button>
                                              </div>
                                            ))}
                                            <button
                                              className="ms-btn link"
                                              style={{ alignSelf: "flex-start", marginLeft: -10 }}
                                              onClick={() =>
                                                patchModels(
                                                  p,
                                                  models.map((x) =>
                                                    x.id === m.id
                                                      ? {
                                                          ...x,
                                                          reasoningLevels: [...(x.reasoningLevels ?? []), "high"],
                                                        }
                                                      : x,
                                                  ),
                                                )
                                              }
                                            >
                                              + 添加档位
                                            </button>
                                            <p className="ms-hint">档位要和厂商文档一致；勾了厂商不认的档，请求会直接报错。</p>
                                          </div>
                                        ) : null}
                                      </div>
                                    ) : null}
                                  </div>
  );
}
