/**
 * 设置 → 模型（新版）：把模板接到真实接口上。
 *
 * 这一层只做三件事：拉数据、转字段、把用户动作转成接口调用。
 * 界面长什么样、校验怎么拦，全在 ModelSettingsTemplate 里（那边是纯展示 + 回调）。
 *
 * 旧模型页（Settings.tsx 里的 model 标签）**原样保留**：新版出问题时还能切回去。
 */

import { useCallback, useEffect, useState } from "react";
import ModelSettingsTemplate, { type ModelEntry, type Provider } from "./ModelSettingsTemplate";
import {
  activateProvider,
  deleteProvider,
  fetchModels,
  listProviders,
  saveProvider,
  type ProviderListItem,
} from "./providersRepo";

function toProvider(item: ProviderListItem): Provider {
  return {
    id: item.id,
    name: item.name,
    kind: item.kind,
    api: item.api,
    baseURL: item.baseURL,
    keyConfigured: item.keyConfigured,
    models: item.models,
    roles: item.roles,
  };
}

export default function ModelSettingsPage({ onChanged }: { onChanged?: () => void }) {
  const [providers, setProviders] = useState<ProviderListItem[] | null>(null);
  const [active, setActive] = useState("");
  const [revision, setRevision] = useState(0);
  const [readOnly, setReadOnly] = useState(false);
  const [savedNotice, setSavedNotice] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      const data = await listProviders();
      setProviders(data.providers);
      setActive(data.active);
      setRevision(data.revision);
      setReadOnly(data.readOnly);
      setError("");
    } catch (err) {
      setProviders([]);
      setError(err instanceof Error ? err.message : "读取供应商失败");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const flash = (text: string) => {
    setSavedNotice(text);
    window.setTimeout(() => setSavedNotice(""), 2600);
  };

  const run = async (action: () => Promise<void>, done: string) => {
    try {
      await action();
      await reload();
      // 供应商一改，主界面的模型清单必须跟着刷新：否则选择器还是旧清单，
      // 用户会以为"选不了"（2026-10-28 真机踩到）。
      onChanged?.();
      flash(done);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  };

  if (providers === null && !error) {
    return <div className="ms-loading">正在读取供应商…</div>;
  }

  // 目录读不出来：照 DSH 的样子给一句原因 + 一个重试按钮，不要只留一行红字
  if (providers === null && error) {
    return (
      <div className="ms">
        <h1 className="ms-title">模型</h1>
        <p className="ms-intro">填入各提供商的 API 密钥即可使用其模型。</p>
        <div className="ms-loadfail">
          <span>加载提供商目录失败：{error}</span>
          <button className="ms-btn ghost sm" onClick={() => void reload()}>
            重试
          </button>
        </div>
      </div>
    );
  }

  const list = providers ?? [];
  const items = list.map(toProvider);

  return (
    <div>
      {error ? <div className="ms-error" style={{ marginBottom: 12 }}>{error}</div> : null}
      <ModelSettingsTemplate
        providers={items}
        activeId={active}
        savedNotice={savedNotice}
        revision={revision}
        fetchRevision={async () => (await listProviders()).revision ?? revision}
        onReload={reload}
        readOnly={readOnly}
        onActivate={(id) => void run(() => activateProvider(id, revision), "已切换当前供应商。")}
        onApplyProvider={async (id, draft) => {
          const base = list.find((x) => x.id === id);
          if (!base) return "这一档已经不存在了，关掉重开后重试。";
          try {
            await saveProvider({
              revision,
              id,
              name: draft.name || id,
              kind: base.kind,
              api: draft.api,
              baseUrl: draft.baseURL,
              apiKey: draft.apiKey || undefined,
              models: draft.models,
              roles: draft.roles,
            });
            await reload();
            onChanged?.();
            flash("已保存 " + (draft.name || id) + "。");
            return null;
          } catch (err) {
            const msg = err instanceof Error ? err.message : "保存失败";
            const status = (err as { status?: number }).status;
            // 409 = 期间别处改过：先把最新配置拉回来，再让用户关掉重开
            if (status === 409 || /409|revision|已被其他地方改动/.test(msg)) {
              await reload();
              return "这份配置在你打开卡片期间被别处改过了。关掉重新打开，改当前的值。";
            }
            return msg;
          }
        }}
        onSaveKey={(id, apiKey) =>
          void run(async () => {
            const base = list.find((p) => p.id === id);
            if (!base) return;
            await saveProvider({ revision,
              id,
              name: base.name,
              kind: base.kind,
              api: base.api,
              baseUrl: base.baseURL,
              apiKey,
              models: base.models,
              roles: base.roles,
            });
          }, "密钥已保存。")
        }
        onDeleteProvider={(id) => void run(() => deleteProvider(id, revision), "已删除。")}
        onRolesChange={(providerId, role, modelId) =>
          void run(async () => {
            const base = list.find((p) => p.id === providerId);
            if (!base) return;
            await saveProvider({ revision,
              id: providerId,
              name: base.name,
              kind: base.kind,
              api: base.api,
              baseUrl: base.baseURL,
              models: base.models,
              roles: { ...base.roles, [role]: modelId },
            });
          }, "角色分工已保存。")
        }
        onDiscover={(id, draft) => {
          const base = list.find((p) => p.id === id);
          return fetchModels({
            baseUrl: draft.baseURL || base?.baseURL || "",
            api: base?.api ?? "openai-chat",
            apiKey: draft.apiKey || undefined,
          });
        }}
        onResetModels={(id) => {
          const base = list.find((p) => p.id === id);
          if (!base || base.kind === "builtin") return;
          // 照 DSH：reset 是**去掉用户覆盖**，回到"目录默认"，不是去上游重拉一份
          void run(async () => {
            await saveProvider({ revision,
              id,
              name: base.name,
              kind: base.kind,
              api: base.api,
              baseUrl: base.baseURL,
              models: base.models.map((m) => ({
                ...m,
                contextWindow: 0,
                maxOutput: 0,
                reasoningLevels: [],
                reasoningValues: {},
              })),
              roles: base.roles,
            });
          }, "已恢复默认模型（能力回到未填写）。");
        }}
        onSaveProvider={(id, patch) => {
          const base = list.find((p) => p.id === id);
          if (!base) return;
          void run(async () => {
            await saveProvider({ revision,
              id,
              name: patch.name ?? base.name,
              kind: base.kind,
              api: patch.api ?? base.api,
              baseUrl: patch.baseURL ?? base.baseURL,
              models: patch.models ?? base.models,
              roles: patch.roles ?? base.roles,
            });
          }, "已更新模型目录。");
        }}
        onDiscoverModels={(input) =>
          fetchModels({ baseUrl: input.baseURL, api: input.api, apiKey: input.apiKey })
        }
        onAddProvider={(draft) =>
          void run(async () => {
            await saveProvider({ revision,
              id: draft.providerId || undefined,
              name: draft.name || draft.providerId,
              kind: "custom",
              api: draft.api,
              baseUrl: draft.baseURL,
              apiKey: draft.apiKey || undefined,
              models: draft.models,
              roles: {},
            });
          }, "已添加供应商。")
        }
      />
    </div>
  );
}