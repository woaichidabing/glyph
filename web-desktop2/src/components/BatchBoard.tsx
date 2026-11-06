import { useEffect, useRef, useState } from "react";
import { useAgent } from "../agent/AgentContext";
import { useStore } from "../store";
import OcrPane from "./OcrPane";
import GradePane from "./GradePane";
import {
  batchSummary, deriveBatch, itemStep, narrowWorkEntry, stageInfo, workPanelMode,
  PANEL_STATES, RETRY_STATES,
  type BatchItem,
} from "../adapters/batch";
import { buildOcrConfirmMessage, buildRetryMessage } from "../adapters/outbound";
import { getRecheckQuota, resetRecheck } from "../adapters/gradeApi";

/** 行内迷你步骤条：已完成绿、当前蓝（失败红）、未到灰 */
function MiniSteps({ item }: { item: BatchItem }) {
  const { index, total, failed } = itemStep(item);
  return (
    <span className="ministeps" aria-hidden>
      {Array.from({ length: total }, (_, i) => {
        const cls = item.status === "done" ? "done"
          : i < index ? "done"
          : i === index ? (failed ? "err" : "cur")
          : "wait";
        return <i key={i} className={cls} />;
      })}
    </span>
  );
}

function DocGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h6"/></svg>
  );
}

function Row({ item, index, open, onClick, onRetry }: {
  item: BatchItem;
  index: number;
  open: boolean;
  onClick?: () => void;
  onRetry?: () => void;
}) {
  const st = stageInfo(item);
  const failed = item.status === "ocr_failed" || item.status === "grade_failed";
  const clickable = onClick !== undefined;
  const Tag = clickable ? "button" : "div";
  return (
    <Tag
      className={"qrow" + (failed ? " is-err" : "") + (open ? " is-open" : "")}
      onClick={onClick}
      type={clickable ? "button" : undefined}
    >
      <span className={"q-thumb" + (item.origin === "text" ? " txt" : "")}>
        {item.origin === "text" ? <DocGlyph />
          : item.url ? <img src={item.url} alt={item.title} />
          : <DocGlyph />}
      </span>
      <span className="q-main">
        <span className="q-t">第 {index + 1} 篇 · {item.title}{item.orphan ? "（未匹配到上传图片）" : ""}</span>
        <span className="q-s">
          <MiniSteps item={item} />
          {failed ? <b>{st.detail}</b> : st.detail}
        </span>
      </span>
      {failed ? (
        <button
          type="button"
          className="q-retry"
          onClick={(e) => { e.stopPropagation(); onRetry?.(); }}
        >
          重试
        </button>
      ) : (
        <span className={"q-stage stage " + st.kind}>
          {st.kind === "run" ? <span className="spin sm" style={{ marginRight: 3 }} /> : <span className="sd" />}
          {st.label}
        </span>
      )}
    </Tag>
  );
}

/** 警告红圈（失败隔离面板用） */
function WarnGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/** 失败详情：只隔离这一篇，可单独重试；onSkip 仅多篇货架传入 */
export function ErrPane({ item, index, onRetry, onSkip }: {
  item: BatchItem;
  index: number;
  onRetry: () => void;
  onSkip?: () => void;
}) {
  const isOcr = item.status === "ocr_failed";
  const headline = isOcr ? "这篇的文字没能识别出来" : "这篇没能批改完";
  return (
    <div className="pane" style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      {/* title 悬停保留后端原始错误，便于反馈 / 排查；主界面只给看得懂的人话 */}
      <div className="err-pane" title={item.error ?? undefined}>
        <span className="err-ic"><WarnGlyph /></span>
        <div>
          <b style={{ fontSize: 13.5 }}>第 {index + 1} 篇{isOcr ? "转写" : "批改"}失败</b>
          <div className="muted" style={{ marginTop: 4, lineHeight: 1.8 }}>
            {headline}，可能是网络抖了一下或服务暂时忙。<br />
            {onSkip
              ? "其他篇的进度和结果都不受影响。"
              : "可以点「重试这一篇」再跑一次；也可以先去忙别的，这条失败会留在这里。"}
          </div>
        </div>
        <div className="row" style={{ marginTop: 6, display: "flex", gap: 8 }}>
          {onSkip ? <button className="btn ghost sm" onClick={onSkip}>先跳过</button> : null}
          <button className="btn primary sm" onClick={onRetry}>重试这一篇</button>
        </div>
      </div>
    </div>
  );
}

/** 非面板态的详情占位（批改中、失败等，后续切片接入）。放大画布也复用 */
export function StagePlaceholder({ item }: { item: BatchItem }) {
  const st = stageInfo(item);
  return (
    <div className="pane" style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      <div className="wb-loading">
        {st.kind === "run" ? <span className="spin" /> : null}
        <div className="ll"><b>{item.title} · {st.label}</b><br />{st.detail}</div>
      </div>
    </div>
  );
}

/**
 * 右翼统一工作面板：
 * - 多篇在途（multi）：批次货架 + 批量条 + 选中篇详情（方案 B）；
 * - 单篇且正卡在核对/拍板（single）：不显示货架，直接把那一篇的面板铺满（单篇零打扰）；
 * - 其他情况（纯问答、单篇处理中、全部定稿）整块隐身。
 */
export default function BatchBoard() {
  const t = useAgent();
  const tray = useStore(s => s.tray);
  const setCanvasMode = useStore(s => s.setCanvasMode);
  const confirmed = useStore(s => s.confirmedOcr);
  const confirmOcr = useStore(s => s.confirmOcr);
  const unconfirmOcr = useStore(s => s.unconfirmOcr);
  const [collapsed, setCollapsed] = useState(
    () => import.meta.env.DEV && new URLSearchParams(location.search).get("demo") === "board-collapse",
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // 各篇未点确认前的编辑草稿（批量确认时取得到）
  const [draft, setDraft] = useState<Record<string, string>>({});

  // 复核配额：新一批 grade 结果出来时先清账本再查（后端约定，账本会话级）；
  // knownCalls 防同批重复 reset，切会话清空。配额放 store，货架与放大画布、批改台共用。
  const setQuota = useStore(s => s.setRecheckQuota);
  const knownGradeCalls = useRef<Set<string>>(new Set());
  useEffect(() => {
    knownGradeCalls.current = new Set();
  }, [t.sessionId]);

  const batch = deriveBatch(t.timeline, confirmed);

  useEffect(() => {
    let fresh = true;
    for (const it of batch.items) {
      if (it.gradeCallId
        && (it.status === "needs_decision" || it.status === "done" || it.status === "grade_failed")
        && !knownGradeCalls.current.has(it.gradeCallId)) {
        knownGradeCalls.current.add(it.gradeCallId);
        void resetRecheck().then(() => getRecheckQuota())
          .then(q => { if (fresh) setQuota(q); });
      }
    }
    return () => { fresh = false; };
  }, [batch.items.map(i => `${i.gradeCallId ?? ""}:${i.status}`).join("|")]);

  /** 单篇失败重试：让 agent 只重跑这一篇（新工具调用由批次模型认领回原 item） */
  const handleRetry = (item: BatchItem, index: number) => {
    t.send(buildRetryMessage(item, index), []);
  };
  const mode = workPanelMode(batch, tray);
  const multi = mode === "multi";
  const openItems = batch.items.filter((i) => i.status !== "done");
  const single = mode === "single";

  const items = batch.items;
  const singleItem = single ? openItems[0] : null;
  const effectiveSelectedId = singleItem?.id ?? selectedId;

  const reviewItems = items.filter((i) => i.status === "needs_review");
  const confirmedItems = items.filter((i) => i.status === "review_confirmed");
  const ocrPending = items.some((i) => i.status === "transcribing");
  const imageItems = items.filter((i) => i.origin === "image");
  const checkedCount = imageItems.filter((i) =>
    i.status !== "needs_review" && i.status !== "transcribing" && i.status !== "ocr_failed",
  ).length;

  /** 把已确认的篇一次性发给 agent 逐篇批改（调用方保证已无待核对/转写中） */
  const sendConfirmed = (texts: Record<string, string>) => {
    const essays = items
      .filter((i) => texts[i.id])
      .map((i) => ({ title: i.title, text: texts[i.id] }));
    if (essays.length === 0) return;
    // 界面确认动作：正文仍发给模型，但对话区只显示一行灰条，不冒充用户输入
    t.send(buildOcrConfirmMessage(essays), [], undefined, "已核对照片作文转写稿，开始批改");
  };

  /** 单篇确认：齐了就发，没齐自动跳到下一张待核对 */
  const handleConfirm = (item: BatchItem, text: string) => {
    confirmOcr(item.id, text);
    const rest = reviewItems.filter((i) => i.id !== item.id);
    if (rest.length === 0 && !ocrPending) {
      sendConfirmed({ ...confirmed, [item.id]: text });
      return;
    }
    if (rest[0]) setSelectedId(rest[0].id);
  };

  /** 一键确认其余待核对篇（按当前草稿/原转写稿）并立即批改 */
  const handleConfirmAll = () => {
    if (ocrPending || reviewItems.length === 0) return;
    const texts: Record<string, string> = { ...confirmed };
    for (const i of reviewItems) {
      texts[i.id] = draft[i.id]?.trim() || i.transcript || "";
      confirmOcr(i.id, texts[i.id]);
    }
    sendConfirmed(texts);
  };

  // DEV 截图钩子：?sel=N 自动打开第 N 篇详情
  const selParam = import.meta.env.DEV ? new URLSearchParams(location.search).get("sel") : null;
  useEffect(() => {
    if (mode !== "multi" || selParam == null) return;
    const idx = Number(selParam) - 1;
    const target = batch.items[idx];
    // 延后一帧：避开自动选中 effect 的首轮落点（待核对/待拍板优先），DEV 截图钩子专用
    const id = setTimeout(() => { if (target) setSelectedId(target.id); }, 0);
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selParam, batch.items.length, mode]);

  // 多篇场景：选中篇被定稿/状态离开面板态后，自动落到下一个还需要处理的篇；
  // 待核对/待拍板优先于失败篇（失败不抢占，用户可主动点失败行）
  useEffect(() => {
    if (mode !== "multi") return;
    const cur = batch.items.find((i) => i.id === selectedId);
    if (cur && [...PANEL_STATES, ...RETRY_STATES].includes(cur.status)) return;
    const next = batch.items.find((i) => PANEL_STATES.includes(i.status))
      ?? batch.items.find((i) => RETRY_STATES.includes(i.status));
    setSelectedId(next ? next.id : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch.items.map(i => i.status).join(","), mode]);

  // 所有 hooks 必须在这个早退之前（mode 会随定稿在 multi/single/none 间切换）
  if (mode === "none") return null;

  const selected = items.find((i) => i.id === effectiveSelectedId) ?? null;
  const showBulkBar = multi && !collapsed && imageItems.some((i) =>
    ["needs_review", "review_confirmed", "transcribing"].includes(i.status));

  const detailPane = selected
    ? selected.status === "needs_decision"
      ? <GradePane item={selected} index={items.indexOf(selected)} />
      : selected.status === "needs_review" || selected.status === "review_confirmed"
        ? (
          <OcrPane
            item={selected}
            index={items.indexOf(selected)}
            busy={ocrPending || reviewItems.some((i) => i.id !== selected.id)}
            onEdit={(txt) => setDraft((d) => ({ ...d, [selected.id]: txt }))}
            onConfirm={(text) => handleConfirm(selected, text)}
            onUnconfirm={() => {
              unconfirmOcr(selected.id);
              setDraft((d) => ({ ...d, [selected.id]: selected.confirmedText ?? selected.transcript ?? "" }));
            }}
          />
        )
        : RETRY_STATES.includes(selected.status)
          ? (
            <ErrPane
              item={selected}
              index={items.indexOf(selected)}
              onRetry={() => handleRetry(selected, items.indexOf(selected))}
              onSkip={multi ? () => {
                const next = items.find((i) => PANEL_STATES.includes(i.status));
                if (next) setSelectedId(next.id);
              } : undefined}
            />
          )
          : <StagePlaceholder item={selected} />
    : (
      <div className="wb-loading">
        <div className="ll">
          <b>{batchSummary(items)}</b><br />
          点任意一篇「待核对 / 待拍板」的篇展开处理；各篇互不等待，单篇失败不影响其他篇。
        </div>
      </div>
    );

  return (
    <aside className="ask3-wb" aria-label="批次进度" style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
      {single ? (
        <button className="back-canvas" onClick={() => setCanvasMode(true)}>
          全屏放大处理这篇 <span className="sp" /> 点击进入 →
        </button>
      ) : null}
      {multi ? (
        <div className={"board" + (collapsed ? " collapsed" : "")}>
          <div className="board-title">
            <b>{items.length} 篇作文</b>
            <span className="bprog">{batchSummary(items)}</span>
            <span className="sp" />
            <span className="b-actions">
              <button className="btn ghost sm" onClick={() => setCanvasMode(true)} title="全屏并排处理各篇">
                放大并排
              </button>
              <button className="btn ghost sm" onClick={() => setCollapsed(v => !v)}>
                {collapsed ? "展开 ⌄" : "折叠 ⌃"}
              </button>
            </span>
          </div>

          {/* 折叠态：一行状态点 */}
          <div className="board-mini">
            {items.map((it, i) => {
              const st = stageInfo(it);
              return (
                <span className="bm-dot" key={it.id}>
                  <i className={st.kind === "wait" ? "" : st.kind} />
                  <b>篇 {i + 1}</b>
                  <em>{st.label}</em>
                </span>
              );
            })}
          </div>

          {/* 展开态：逐篇行（待核对/待拍板/失败可点进详情） */}
          {items.map((it, i) => (
            <Row
              key={it.id}
              item={it}
              index={i}
              open={effectiveSelectedId === it.id}
              onClick={!collapsed && [...PANEL_STATES, ...RETRY_STATES].includes(it.status)
                ? () => setSelectedId(it.id) : undefined}
              onRetry={() => handleRetry(it, i)}
            />
          ))}
        </div>
      ) : null}

      {multi && !collapsed && items.some((i) => RETRY_STATES.includes(i.status)) ? (
        <div className="bulk-bar bulk-err">失败只隔离这一篇：重试不影响其他篇的进度，已完成的结果不会丢</div>
      ) : null}

      {multi && !collapsed && showBulkBar ? (
        <div className="bulk-bar">
          <span>
            {ocrPending
              ? `还有照片在转写…核对完才会批改 · 已确认 ${confirmedItems.length}/${imageItems.length} 张`
              : `照片逐张核对完才会批改 · 已核对 ${checkedCount}/${imageItems.length} 张`}
          </span>
          <span className="sp" />
          <button
            className="btn primary sm"
            disabled={ocrPending || reviewItems.length === 0}
            onClick={handleConfirmAll}
          >
            全部确认并批改{reviewItems.length > 0 ? `（${reviewItems.length} 张按原转写稿）` : ""}
          </button>
        </div>
      ) : null}

      {/* 详情区：单篇模式铺满；多篇折叠时收起 */}
      {multi ? (!collapsed ? detailPane : null) : detailPane}
    </aside>
  );
}

/**
 * 窄屏（<1020px）专属：媒体查询把右翼批阅台 / OCR 核对面板整块藏了，
 * 就在输入区上方给一个醒目的入口条（打开 / 失败时重试），点了进放大并排画布全屏处理。
 * 宽屏由 CSS 直接 display:none，不占地方；显示口径与右翼面板一致（narrowWorkEntry）。
 */
export function NarrowWorkEntryBar() {
  const t = useAgent();
  const tray = useStore(s => s.tray);
  const confirmed = useStore(s => s.confirmedOcr);
  const setCanvasMode = useStore(s => s.setCanvasMode);
  const entry = narrowWorkEntry(deriveBatch(t.timeline, confirmed), tray);
  if (!entry) return null;
  return (
    <button
      type="button"
      className={"narrow-entry tone-" + entry.tone}
      onClick={() => setCanvasMode(true)}
      title="窗口较窄，批阅台已收起到全屏，点这里打开处理"
    >
      <span className={"ne-dot " + entry.tone} aria-hidden />
      <span className="ne-title">{entry.title}</span>
      <span className="ne-status">{entry.statusText}</span>
      <span className="ne-go">{entry.actionText} →</span>
    </button>
  );
}
