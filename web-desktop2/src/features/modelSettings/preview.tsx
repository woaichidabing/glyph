import { useState } from "react";
import { createRoot } from "react-dom/client";
import "../../styles/tokens.css";
import ModelSettingsTemplate, { type Provider } from "./ModelSettingsTemplate";

const PROVIDERS: Provider[] = [
  {
    id: "qwen3ning",
    name: "qwen3ning",
    kind: "custom",
    baseURL: "https://ws-ohzvhzoeql91ys88.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    api: "openai-responses",
    keyConfigured: true,
    modelsCustomized: true,
    models: [
      { id: "kimi/kimi-k3.1-pro-highspeed", label: "kimi/kimi-k3.1-pro-highspeed", contextWindow: 1048576, maxOutput: 32768, inputs: ["text", "image"] },
      { id: "kimi/kimi-k3.1-pro", label: "kimi/kimi-k3.1-pro", contextWindow: 1000000, maxOutput: 32768, inputs: ["text", "image"] },
      { id: "MiniMax/MiniMax-M3.1-Flash", label: "MiniMax/MiniMax-M3.1-Flash", contextWindow: 262144, maxOutput: 32768, inputs: ["text"] },
      { id: "xiaomi/mimo-v2.6-pro-ultraspeed", label: "xiaomi/mimo-v2.6-pro-ultraspeed", contextWindow: 262144, maxOutput: 32768, inputs: ["text"] },
      { id: "deepseek-v4.1-flash-1017", label: "deepseek-v4.1-flash-1017", contextWindow: 1000000, maxOutput: 256000, inputs: ["text"] },
    ],
  },
  {
    id: "qwen",
    name: "千问（DashScope）",
    kind: "builtin",
    baseURL: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    api: "openai-responses",
    keyConfigured: true,
    envHint: "DASHSCOPE_API_KEY",
    models: [
      { id: "qwen3.8-max", label: "千问 3.8 Max", contextWindow: 1048576, maxOutput: 256000, inputs: ["text", "image"] },
      { id: "qwen3-vl-flash", label: "Qwen3 VL Flash", contextWindow: 262144, maxOutput: 32768, inputs: ["text", "image"] },
      { id: "qwen3.7-plus", label: "千问 3.7 Plus", contextWindow: 1048576, maxOutput: 64000, inputs: ["text"] },
    ],
  },
  {
    id: "relay",
    name: "自建 DeepSeek 中转",
    kind: "custom",
    baseURL: "https://relay.example.com/v1",
    api: "openai-responses",
    keyConfigured: true,
    isDefault: true,
    models: [
      { id: "deepseek-v4.1-flash", label: "DeepSeek V4.1 Flash", contextWindow: 1048576, maxOutput: 256000, inputs: ["text"] },
      { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro", contextWindow: 1048576, maxOutput: 256000, inputs: ["text"] },
    ],
  },
  {
    id: "openai",
    name: "OpenAI 兼容（备用）",
    kind: "custom",
    baseURL: "https://api.example.com/v1",
    api: "openai-chat",
    keyConfigured: false,
    envHint: "OPENAI_API_KEY",
    models: [],
  },
];

function Preview() {
  const initialPanel = new URLSearchParams(location.search).get("panel") === "dark" ? "dark" : "light";
  const [panel, setPanel] = useState<"light" | "dark">(initialPanel);
  const [saved, setSaved] = useState("");
  const [providers, setProviders] = useState(PROVIDERS);

  const apply = (next: "light" | "dark") => {
    setPanel(next);
    document.documentElement.dataset.panel = next;
  };
  document.documentElement.dataset.panel = panel;

  return (
    <div style={{ padding: "24px 28px 80px", background: "rgb(var(--app-rgb))", minHeight: "100vh" }}>
      <div style={{ display: "flex", gap: 10, marginBottom: 18, alignItems: "center" }}>
        <span className="ms-seg">
          <button aria-pressed={panel === "light"} onClick={() => apply("light")}>
            浅色
          </button>
          <button aria-pressed={panel === "dark"} onClick={() => apply("dark")}>
            深色
          </button>
        </span>
        <span style={{ fontSize: 12, color: "rgb(var(--ink)/.45)" }}>模板预览（mock 数据，不连后端）</span>
      </div>
      <ModelSettingsTemplate
        providers={providers}
        savedNotice={saved}
        initialOpenId={new URLSearchParams(location.search).get("open") ?? undefined}
        onSaveKey={(id, key) => {
          setProviders((cur) => cur.map((p) => (p.id === id ? { ...p, keyConfigured: true, envHint: undefined } : p)));
          setSaved("已保存 " + providers.find((p) => p.id === id)?.name + "。");
          setTimeout(() => setSaved(""), 2600);
          void key;
        }}
        onDeleteProvider={(id) => setProviders((cur) => cur.filter((p) => p.id !== id))}
        onRolesChange={(providerId, role, modelId) => {
          setProviders((cur) =>
            cur.map((p) =>
              p.id === providerId
                ? { ...p, roles: { ...(p.roles ?? {}), [role]: modelId } }
                : p,
            ),
          );
        }}
        onAddProvider={() => setSaved("添加（mock）")}
        onDiscover={async () => []}
        onSaveProvider={() => undefined}
      />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Preview />);

// 预览用：自动点开第一档的「编辑」，方便截图看编辑器
setTimeout(() => {
  const btn = Array.from(document.querySelectorAll<HTMLElement>("button")).find((b) => b.textContent?.trim() === "编辑");
  btn?.click();
}, 500);
