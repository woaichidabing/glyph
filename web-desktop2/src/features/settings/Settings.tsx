import { useEffect, useRef, useState } from "react";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import {
  getAgentConfig,
  getBrainModels,
  saveBrainChoice,
  saveDefaultProviderKey,
  saveRoleModels,
  saveSpeakingStyle,
  switchToDefaultProvider,
  type AgentConfig,
  type BrainModel,
  type ModelSource,
  type SpeakingStyle,
} from "./configRepo";
import ModelSettingsPage from "../modelSettings/ModelSettingsPage";
import type { BrainApi } from "./useBrain";
import SlotIcon from "./SlotIcon";
import UsagePane from "./UsagePane";
import StoragePane from "./StoragePane";
import AppearancePane from "./AppearancePane";

/**
 * 默认档看图槽的常见千问视觉型号（未逐个实测，换上试一把就知道；清单外的型号可手动填）。
 * 刻意放前端常量而不是后端清单：它们只是"名字候选"，能力判定与生效永远在后端。
 */
const DEFAULT_VISION_CATALOG: string[] = [
  "qwen3-vl-plus",
  "qwen3-vl-max",
  "qwen2.5-vl-72b-instruct",
];

type Tab = "appearance" | "providers" | "assistant" | "usage" | "storage";

/** 每一页的标题与一句说明（照 DSH 的版式：标题 16/500 + 说明 14 灰） */
const PAGE_HEAD: Record<Tab, { title: string; sub: string }> = {
  appearance: { title: "外观", sub: "主题、字体、行宽与批改标记。只影响这台机器上的显示。" },
  providers: { title: "模型", sub: "" },
  assistant: { title: "助手", sub: "说话风格这类回答偏好。" },
  usage: { title: "用量", sub: "本机的 token 用量与额度状态。" },
  storage: { title: "存储与备份", sub: "学习数据放哪、占用多少、怎么迁移。" },
};

const NAV: { id: Tab; label: string; icon: JSX.Element }[] = [
  {
    id: "appearance",
    label: "外观",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 4v16" strokeLinecap="round" />
      </svg>
    ),
  },
  {
    id: "providers",
    label: "模型",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <rect x="6" y="6" width="12" height="12" rx="2" />
        <path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" />
      </svg>
    ),
  },
  {
    id: "assistant",
    label: "助手",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
      </svg>
    ),
  },
  {
    id: "storage",
    label: "存储与备份",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <ellipse cx="12" cy="5" rx="8" ry="3" />
        <path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" />
        <path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
      </svg>
    ),
  },
  {
    id: "usage",
    label: "用量",
    icon: (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M3 3v18h18" />
        <path d="M7 15v3M12 10v8M17 6v12" />
      </svg>
    ),
  },
];

/** 三档说话风格：文案 / 示例与后端 persona、设计稿逐字对齐。 */
const STYLE_OPTIONS: {
  id: SpeakingStyle;
  name: string;
  desc: string;
  eg: string;
  isDefault?: boolean;
}[] = [
  {
    id: "natural",
    name: "自然",
    desc: "好好说话，不端着、也不硬搞笑",
    eg: "这句意思清楚，但有三处要改。改完是 I really liked playing basketball yesterday. 有 yesterday，动词就用过去式。",
    isDefault: true,
  },
  {
    id: "witty",
    name: "风趣",
    desc: "会开玩笑、爱打比方，写错也没压力",
    eg: "意思我秒懂，就是语法在里头打了一架，还输了。一共三处，咱挨个拉架——改完：I really liked playing basketball yesterday. 口诀：yesterday 一出场，动词就得乖乖变过去式。",
  },
  {
    id: "calm",
    name: "冷静",
    desc: "只给结论和重点，用最少的话",
    eg: "三处错。I really liked playing basketball yesterday.（yesterday→过去式 liked；very like→really like；like play→like playing）",
  },
];

const STYLE_HINT = "切换后从下一条消息开始生效。无论哪种风格，批改的分数、错误判定和正确答案都完全一样。";

/**
 * 设置弹窗（分层：外观 / 模型 / 助手）。
 * - 外观：主题本地持久化（store 负责），点选即时生效；
 * - 模型：API Key 只存本机后端，前端从不持有明文回显，需显式保存；
 * - 助手：说话风格存后端、随 runtime 重建从下一轮生效，点选即时保存（失败乐观回滚）。
 */
export default function Settings({ open, onClose, brain }: { open: boolean; onClose: () => void; brain: BrainApi }) {
  // 走查用：?tab=models|providers|assistant|usage|appearance 直接把那一页打开（仅 dev）
  const [tab, setTab] = useState<Tab>(() => {
    if (!import.meta.env.DEV) return "appearance";
    const wanted = new URLSearchParams(location.search).get("tab");
    return (wanted as Tab | null) ?? "appearance";
  });

  const [apiKey, setApiKey] = useState("");
  const [hasKey, setHasKey] = useState<boolean | null>(null);
  // 默认千问档自己的 Key 状态：自定义档下 hasKey 是自定义 Key，不能拿来判断
  const [defaultHasKey, setDefaultHasKey] = useState(false);
  const [switchingBack, setSwitchingBack] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  // 模型来源分段（默认千问 / 自定义端点）；仅本地视图切换，真正保存由各档按钮触发
  // 完整配置（含自定义档回显的模型清单 / 能力），供自定义面板初始化
  const [cfg, setCfg] = useState<AgentConfig | null>(null);

  const [style, setStyle] = useState<SpeakingStyle>("natural");
  const [styleBusy, setStyleBusy] = useState(false);
  const [styleMsg, setStyleMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);


  const modalRef = useRef<HTMLDivElement | null>(null);
  useFocusTrap(modalRef, open);

  // 每次打开：重置到外观页（dev 下可被 ?tab= 覆盖），向后端问一句"配没配 key、当前什么说话风格"
  useEffect(() => {
    if (!open) return;
    const wanted = import.meta.env.DEV
      ? (new URLSearchParams(location.search).get("tab") as Tab | null)
      : null;
    let cancelled = false;
    setTab(wanted ?? "appearance");
    setMsg(null);
    setStyleMsg(null);
    setCfg(null);
    getAgentConfig()
      .then((cfg) => {
        if (cancelled) return;
        setHasKey(cfg.hasKey);
        setDefaultHasKey(cfg.defaultHasKey);
        setStyle(cfg.speakingStyle);
        setCfg(cfg);
      })
      .catch(() => {
        if (cancelled) return;
        setHasKey(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Esc 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  /** 切说话风格：乐观不需要，失败只报错不写缓存 */
  const pickStyle = async (id: SpeakingStyle) => {
    setStyleBusy(true);
    setStyleMsg(null);
    try {
      const next = await saveSpeakingStyle(id);
      setStyle(next.speakingStyle);
      setStyleMsg({ kind: "ok", text: "已保存，从下一轮对话开始生效。" });
    } catch (e) {
      setStyleMsg({ kind: "err", text: e instanceof Error ? e.message : "保存失败，请稍后重试。" });
    } finally {
      setStyleBusy(false);
    }
  };

  return (
    <div
      className={"set-mask" + (open ? " open" : "")}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div ref={modalRef} className={"set-modal set-modal-tabs" + (tab === "storage" ? " set-modal-wide" : "")} role="dialog" aria-modal="true" aria-label="设置">
        <div className="set-head">
          <h3>设置</h3>
          <button className="set-x" aria-label="关闭设置" onClick={onClose}>
            ×
          </button>
        </div>

        <div className="set-shell">
          <nav className="set-nav">
            {NAV.map((item, index) => (
              <button
                key={item.id}
                type="button"
                className={"set-nav-item" + (tab === item.id ? " on" : "")}
                aria-current={tab === item.id ? "page" : undefined}
                data-autofocus={index === 0 ? true : undefined}
                onClick={() => {
                  setTab(item.id);
                  setMsg(null);
                  setStyleMsg(null);
                }}
              >
                {item.icon}
                <span>{item.label}</span>
              </button>
            ))}
          </nav>

          <div className="set-pane">
            {tab !== "providers" ? (
              <>
                <h2 className="set-h1">{PAGE_HEAD[tab].title}</h2>
                <p className="set-sub">{PAGE_HEAD[tab].sub}</p>
              </>
            ) : null}

            {tab === "usage" && <UsagePane />}

            {tab === "storage" && <StoragePane />}

            {tab === "providers" && <ModelSettingsPage onChanged={() => void brain.refresh()} />}

            {tab === "appearance" && <AppearancePane />}


            {tab === "assistant" && (
              <div className="set-field" role="radiogroup" aria-label="说话风格">
                <label>说话风格</label>
                {STYLE_OPTIONS.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={"style-card" + (style === s.id ? " on" : "")}
                    role="radio"
                    aria-checked={style === s.id}
                    disabled={styleBusy}
                    onClick={() => void pickStyle(s.id)}
                  >
                    <span className="style-radio" aria-hidden="true" />
                    <span className="sc-main">
                      <span className="sc-top">
                        <b>{s.name}</b>
                        {s.isDefault && <span className="deftag">默认</span>}
                      </span>
                      <span className="sc-desc">{s.desc}</span>
                      <span className="sc-eg">
                        <span className="q">示例：</span>
                        {s.eg}
                      </span>
                    </span>
                  </button>
                ))}
                <div className="set-hint">{STYLE_HINT}</div>
                {styleMsg && <div className={"set-msg " + styleMsg.kind}>{styleMsg.text}</div>}
              </div>
            )}
          </div>
        </div>

        {tab !== "appearance" && (
          <div className="set-foot">
            <span className="set-foot-note">设置保存在本机 · 版本 {__APP_VERSION__}</span>
            <button type="button" className="save" onClick={onClose}>
              完 成
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
