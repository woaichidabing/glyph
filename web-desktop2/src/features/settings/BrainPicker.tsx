import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Agent } from "../../agent/AgentContext";
import type { BrainModel } from "./configRepo";
import { effortLabel, effortShortLabel, type Effort } from "./brainRepo";
import { modelChipText, shouldShowEffortChip, railX, pickIndex, sheetHeight } from "./brainPickerLabels";
import {
  badgeLetter,
  brandForModel,
  logoForBrand,
  resolveBrand,
  type BrainBrand,
} from "../../adapters/brainLogo";
import { effectiveWindow, supportsMaxContext, type ContextMode } from "./contextPrefRepo";
import ContextInspector from "./ContextInspector";

const R = 6.5;
const C = 2 * Math.PI * R;
const LEVEL_ORDER: Effort[] = ["off", "low", "medium", "high", "xhigh", "max"];
/** 滑杆两端各内缩多少（样稿量到的 11px）—— 滑柄 16px 宽，不内缩会戳出轨道 */
const INSET = 11;

const LEVEL_DESC: Record<Effort, string> = {
  off: "不发思考参数，回答最快",
  low: "更快，简单问题够用",
  medium: "均衡，日常推荐",
  high: "难题多想一会儿，更慢更稳",
  xhigh: "长文推理、复杂纠错再往上推一档；更慢，也更耗额度",
  max: "最慢最贵，留给最难的题；厂商不支持这一档时会直接报错",
};
const CTX_DESC: Record<ContextMode, string> = {
  standard: "日常对话、作文、练习够用；更早整理前文，更省额度、响应更快。",
  max: "能装下长对话和整本材料、少丢前文；更耗额度、响应也会慢一些。",
};

type View = "closed" | "models" | "levels" | "context";

/** 模型标志：内置四家有官方图就用图；其余（OpenAI/Claude/Gemini/Grok/自定义）用品牌色首字母圆，绝不配错图 */
function BrandMark({ brand, id, className }: { brand: BrainBrand; id: string; className?: string }) {
  const logo = logoForBrand(brand);
  if (logo) return <img className={className} src={logo} alt="" aria-hidden />;
  if (brand === "unknown") return null;
  return (
    <span className={`${className ?? ""} bp-badge brand-${brand}`} aria-hidden>
      {badgeLetter(id)}
    </span>
  );
}


const IcChevron = ({ left }: { left?: boolean }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round"
    style={left ? { transform: "rotate(180deg)" } : undefined}><path d="m9 18 6-6-6-6" /></svg>
);
const IcCheck = ({ className }: { className?: string }) => (
  <svg className={className} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
);
const IcSignal = ({ className }: { className?: string }) => (
  <svg className={className} width="15" height="15" viewBox="0 0 24 24" fill="currentColor" stroke="none">
    <rect x="3.5" y="13.5" width="3.2" height="7" rx="1.2" />
    <rect x="10.4" y="9" width="3.2" height="11.5" rx="1.2" />
    <rect x="17.3" y="4" width="3.2" height="16.5" rx="1.2" />
  </svg>
);

/**
 * 面板一打开，把**当前选中的模型**滚进可视区。
 *
 * 为什么需要：型号有 9 个，列表限高 196px 只装得下 4 个半；
 * 选中的那个排在第 5 位以后时，打开面板根本看不见它在哪 —— 用户以为没选中。
 */
export default function BrainPicker({ agent }: { agent: Agent }) {
  const brain = agent.brain;
  const busy = agent.running; // 回答中禁切（后端此时换脑会 409）
  const usage = agent.contextUsage;

  // 走查用：?ctx=1 直接把上下文面板打开（仅 dev 构建；生产里这个参数无效）
  // 走查用：?ctx=1 打开上下文面板，?bp=models / ?bp=levels 打开模型清单 / 强度滑杆（仅 dev）
  const [view, setView] = useState<View>(() => {
    if (!import.meta.env.DEV) return "closed";
    const q = new URLSearchParams(location.search);
    if (q.get("ctx") === "1") return "context";
    const wanted = q.get("bp");
    return wanted === "models" || wanted === "levels" ? (wanted as View) : "closed";
  });
  const anchorRef = useRef<HTMLSpanElement>(null);
  // 弹层方向：默认向下（顶部工具条场景）；只有下方放不下、且上方更宽裕时才向上，
  // 否则在顶部工具条固定向上弹会整体飞出视口。
  const [drop, setDrop] = useState<"up" | "down">("down");

  const models: BrainModel[] = brain.catalog?.models ?? [];
  const current = useMemo(
    () => models.find((m) => m.id === brain.choice?.model) ?? null,
    [models, brain.choice],
  );
  const levels = current?.reasoning_levels ?? [];
  const supportsReason = levels.length > 0;
  // 标称窗口不超过标准 30 万的模型开 Max 没增益，整块隐藏；
  // 自定义档（窗口由用户自己填）也不再显示这一档（2026-10-29 产品所有者定）
  const canMax =
    (brain.catalog?.context_mode_applies ?? true) && supportsMaxContext(current?.context_window);

  useLayoutEffect(() => {
    if (view === "closed" || !anchorRef.current) return;
    const r = anchorRef.current.getBoundingClientRect();
    // 高度不再写死：模型面板 / 强度面板 / 上下文面板差得远，估错了面板会被视口切掉，
    // 切掉的部分连滚轮都够不着（这就是「滚不动」的一半原因）。
    const need = sheetHeight({
      view,
      listMaxH: 196,
      hasBottomBlock: canMax,
    });
    const below = window.innerHeight - r.bottom;
    const above = r.top;
    setDrop(below >= need || below >= above ? "down" : "up");

    // 打开模型面板时把**当前选中项**滚进可视区：9 个型号、限高 196px 只看得到 4 个半，
    // 选中的排在第 5 位以后时打开面板根本看不见它在哪。（要在 setDrop 之后量，
    // 因为气泡的高度依赖 drop 类。这里直接按行元素算，不依赖布局尺寸，够稳。）
    if (view === "models" && modelsRef.current) {
      const box = modelsRef.current;
      const row = box.querySelector<HTMLElement>('[data-current="1"]');
      if (row) {
        const top = row.offsetTop - box.offsetTop;
        const bottom = top + row.offsetHeight;
        if (top < box.scrollTop) box.scrollTop = top;
        else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight;
      }
    }
  }, [view, canMax]);


  // 开始回答（busy）时强制收起浮层；点外部 / Esc 也收起
  useEffect(() => {
    if (busy) setView("closed");
  }, [busy]);
  useEffect(() => {
    if (view === "closed") return;
    const onDown = (e: MouseEvent) => {
      if (anchorRef.current && !anchorRef.current.contains(e.target as Node)) setView("closed");
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setView("closed"); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [view]);

  /* ---- 上下文用量环：分母随档位（标准 30 万 / Max 100 万，且不超型号真实窗口）；无用量画空环 ---- */
  const windowTokens = effectiveWindow(brain.contextMode, current?.context_window);
  const usedTokens = usage && typeof usage.tokens === "number" ? usage.tokens : null;
  const ratio = usedTokens === null ? null : Math.min(1, Math.max(0, usedTokens / windowTokens));
  const ringColor =
    ratio === null ? "transparent"
      : ratio >= 0.9 ? "var(--sem-red)"
        : ratio >= 0.7 ? "var(--sem-yellow-ink)"
          : "rgb(var(--ink) /.45)";
  const meterColor = ratio === null ? "rgb(var(--ink) /.4)" : ringColor;
  const ringTitle = ratio === null ? "上下文用量（暂无）" : `上下文已用约 ${Math.round(ratio * 100)}%`;
  const meterText = ratio === null ? "" : `${Math.round(ratio * 100)}%`;
  const chipBrand = current
    ? resolveBrand(current.brand, current.id)
    : brandForModel(brain.choice?.model);
  const modelName = current?.label ?? brain.choice?.model ?? "模型";
  const effort = effortLabel(brain.choice?.effort);

  /* ---- 思考深度滑杆：算好档位顺序 + 轨道像素位置 ---- */
  const ordered = useMemo<Effort[]>(() => LEVEL_ORDER.filter((l) => levels.includes(l)), [levels]);
  const effIdx = Math.max(0, ordered.indexOf((brain.choice?.effort ?? "") as Effort));
  const railRef = useRef<HTMLSpanElement>(null);
  /** 模型列表容器。打开面板时用它把**当前选中的那行**滚进可视区 */
  const modelsRef = useRef<HTMLSpanElement>(null);
  const [railWidth, setRailWidth] = useState(0);

  // 轨道宽度由 CSS 决定，只能量；面板打开时量一次，窗口大小变了重量
  useLayoutEffect(() => {
    if (view !== "levels" || !railRef.current) return;
    const measure = () => setRailWidth(railRef.current?.clientWidth ?? 0);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [view]);

  const railPos = railX(effIdx, ordered.length, railWidth, INSET);
  const pickLevel = (l: Effort | undefined) => {
    if (!l || !brain.choice || l === brain.choice.effort) return;
    void brain.select(brain.choice.model, l);
  };
  const applyPick = (clientX: number) => {
    const el = railRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    pickLevel(ordered[pickIndex(clientX - r.left, ordered.length, r.width, INSET)]);
  };

  return (
    <span className="bp-anchor" ref={anchorRef}>
      {/* 上下文用量环：没有真实用量（新会话 / 刚清空）时不渲染——空灰圈没有信息量还像没加载完 */}
      {ratio !== null ? (
        <button
          type="button"
          className={"bp-ring-btn" + (view === "context" ? " on" : "")}
          title={ringTitle + "（点开看窗口被谁占着）"}
          aria-label={ringTitle}
          aria-expanded={view === "context"}
          onClick={() => setView((v) => (v === "context" ? "closed" : "context"))}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
            <circle className="bp-ring-bg" cx="8" cy="8" r={R} />
            <circle className="bp-ring-fg" cx="8" cy="8" r={R}
              style={{
                stroke: ringColor,
                strokeDasharray: C.toFixed(2),
                strokeDashoffset: (C * (1 - (ratio as number))).toFixed(2),
              }} />
          </svg>
        </button>
      ) : null}

      {/* 模型与思考强度是**两颗独立 chip**（定稿样稿 §5.6）：各开各的浮层。
          强度这颗在型号不支持思考时整颗不出现 —— 不给的东西不占地方。 */}
      <button type="button" className="bp-chip" disabled={busy || !brain.ready}
        aria-haspopup="menu" aria-expanded={view === "models"}
        title={busy ? "正在回答，先停下再换模型" : "选择模型"}
        onClick={() => setView((v) => (v === "models" ? "closed" : "models"))}>
        <BrandMark brand={chipBrand} id={brain.choice?.model ?? ""} className="bp-logo" />
        <span className="bp-chip-label">
          {modelChipText({ label: current?.label ?? null, effort: effort ?? null, ready: brain.ready })}
        </span>
        <IcChevron />
      </button>
      {shouldShowEffortChip(levels) ? (
        <button type="button" className="bp-chip bp-chip-effort" disabled={busy}
          aria-haspopup="dialog" aria-expanded={view === "levels"}
          title={busy ? "正在回答，先停下再换" : "思考强度"}
          onClick={() => setView((v) => (v === "levels" ? "closed" : "levels"))}>
          <span className="bp-chip-label">{effortShortLabel(brain.choice?.effort)}</span>
          <IcChevron />
        </button>
      ) : null}

      {view !== "closed" ? (
        <span
          className={
            "bp-sheet glass " +
            (drop === "up" ? "bp-sheet-up" : "bp-sheet-down") +
            (view === "context" ? " bp-sheet-ctx" : "")
          }
          role="dialog"
          aria-label={view === "levels" ? "思考强度" : "选择模型"}
        >
          {brain.catalog?.selected_missing ? (
            <span className="bp-error bp-warn" role="status">
              所选模型「{brain.catalog.replaced_from}」已不在这一档的清单里，已切回当前模型。
            </span>
          ) : null}
          {brain.error ? <span className="bp-error" role="alert">{brain.error}</span> : null}

          {view === "context" ? (
            <ContextInspector usedTokens={usedTokens} windowTokens={windowTokens} />
          ) : view === "models" ? (
            <>
              <span className="bp-models" ref={modelsRef}>
                {models.map((m) => {
                  const brand = resolveBrand(m.brand, m.id);
                  return (
                    <button key={m.id} type="button"
                      className={"bp-row" + (brain.choice?.model === m.id ? " on" : "")}
                      data-current={brain.choice?.model === m.id ? "1" : undefined}
                      onClick={() => { void brain.select(m.id, brain.choice?.effort); setView("closed"); }}>
                      <BrandMark brand={brand} id={m.id} className="bp-logo" />
                      <span className="bp-namecol">
                        <span className="bp-name">{m.label || m.id}</span>
                        {m.desc ? <span className="bp-desc">{m.desc}</span> : null}
                      </span>
                      {brain.choice?.model === m.id ? (
                        <span className="bp-check-ic" aria-hidden="true">✓</span>
                      ) : null}
                    </button>
                  );
                })}
              </span>
              {/* 「推理强度」不再放在模型浮层里 —— 框外那颗独立的强度 chip 才是它的入口。
                  两处入口会让用户以为它们是两件事。 */}
              {canMax ? (
                <>
                  <span className="bp-divider" />
                  <div className="bp-ctxmax">
                <div className="bp-cm-head">
                  <span className="bp-cm-title">
                    更大上下文{brain.contextMode === "max" ? <span className="bp-cm-max">MAX</span> : null}
                  </span>
                  <button type="button"
                    className={"bp-switch" + (brain.contextMode === "max" ? " is-on" : "")}
                    role="switch" aria-checked={brain.contextMode === "max"}
                    title={brain.contextMode === "max" ? "已开启更大上下文，点击回到标准" : "开启更大上下文：装得下长材料，但更耗额度、更慢"}
                    onClick={() => void brain.selectContextMode(brain.contextMode === "max" ? "standard" : "max")}>
                    <span className="bp-switch-knob" />
                  </button>
                </div>
                <div className="bp-cm-meter">
                  <span className="bp-cm-track">
                    <span className="bp-cm-fill"
                      style={ratio === null ? { width: "0%" } : { width: `${Math.round(ratio * 100)}%`, background: meterColor }} />
                  </span>
                  <span className="bp-cm-num">{meterText}</span>
                </div>
                <p className="bp-cm-desc">{CTX_DESC[brain.contextMode]}</p>
                  </div>
                </>
              ) : null}
            </>
          ) : (
            <>
              {/* 思考深度滑杆 —— 照定稿样稿 §5.6（CC 同款）：
                  顶部「思考深度 + 当前值 + ?」，下面「更快 ←→ 更聪明」一条轨，
                  几档就有几个刻度，点 / 拖 / 方向键都能动，就近磁吸。 */}
              <span className="effp-head">
                <span className="effp-t">思考深度</span>
                <span className="effp-v">{effortShortLabel(brain.choice?.effort) || "—"}</span>
                <button type="button" className="effp-q" onClick={(e) => e.stopPropagation()}
                  title="档位越高，难题想得越久，也越慢">?</button>
              </span>
              {ordered.length > 1 ? <span className="effp-ends"><span>更快</span><span>更聪明</span></span> : null}
              <span className="effp-rail" role="slider" tabIndex={0}
                aria-label="思考深度"
                aria-valuemin={0}
                aria-valuemax={Math.max(0, ordered.length - 1)}
                aria-valuenow={effIdx}
                aria-valuetext={effortShortLabel(brain.choice?.effort) || ""}
                ref={railRef}
                onPointerDown={(e) => {
                  e.currentTarget.setPointerCapture(e.pointerId);
                  applyPick(e.clientX);
                }}
                onPointerMove={(e) => {
                  if (e.currentTarget.hasPointerCapture(e.pointerId)) applyPick(e.clientX);
                }}
                onKeyDown={(e) => {
                  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
                  e.preventDefault();
                  const next = Math.min(ordered.length - 1,
                    Math.max(0, effIdx + (e.key === "ArrowRight" ? 1 : -1)));
                  pickLevel(ordered[next]);
                }}>
                <span className="effp-fill" style={{ width: railPos + "px" }} />
                {/* 刻度按真实档位数摊开：以前只写了 3 档（首、中、尾），
                    六档时中间四颗全叠在 50% 上，看起来就是"只有 3 个点"（2026-10-29 用户指出） */}
                {ordered.map((l, k) => (
                  <span
                    key={l}
                    className="effp-tick"
                    style={{ left: railX(k, ordered.length, railWidth, INSET) }}
                  />
                ))}
                <span className="effp-thumb" style={{ left: railPos + "px" }} />
              </span>
              <span className="effp-foot">{LEVEL_DESC[ordered[effIdx]] ?? ""}</span>
            </>
          )}
        </span>
      ) : null}
    </span>
  );
}
