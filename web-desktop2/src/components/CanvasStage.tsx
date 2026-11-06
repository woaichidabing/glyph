import { useEffect, useState } from "react";
import { useAgent } from "../agent/AgentContext";
import { useStore } from "../store";
import OcrPane from "./OcrPane";
import GradePane from "./GradePane";
import { StagePlaceholder, ErrPane } from "./BatchBoard";
import {
  canvasGridClass, deriveBatch, stageInfo,
  type BatchItem,
} from "../adapters/batch";
import { buildOcrConfirmMessage, buildRetryMessage } from "../adapters/outbound";

/**
 * 放大并排画布（方案 C，仅 OCR 核对 / 批阅时刻全屏出现）：
 * - 并排：开放中的篇各占一栏（最多 3），互不等待；
 * - 逐个看：单栏铺满 + 上一篇/下一篇翻页（OCR 双栏、批阅台都有足够宽度）。
 * 货架视图（BatchBoard）仍挂载着，配额 effect 不中断；本组件只负责展示与操作。
 */
export default function CanvasStage() {
  const t = useAgent();
  const confirmed = useStore(s => s.confirmedOcr);
  const confirmOcr = useStore(s => s.confirmOcr);
  const unconfirmOcr = useStore(s => s.unconfirmOcr);
  const canvasMode = useStore(s => s.canvasMode);
  const setCanvasMode = useStore(s => s.setCanvasMode);
  const [layout, setLayout] = useState<"split" | "single">(() =>
    import.meta.env.DEV && new URLSearchParams(location.search).get("layout") === "single" ? "single" : "split");
  const [focus, setFocus] = useState(() => {
    const f = Number(new URLSearchParams(location.search).get("focus") ?? "0");
    return Number.isFinite(f) && f > 0 ? f : 0;
  });
  // 各篇未点确认前的编辑草稿（批量确认时取得到）
  const [draft, setDraft] = useState<Record<string, string>>({});

  const batch = deriveBatch(t.timeline, confirmed);
  const items = batch.items;
  const openItems = items.filter((i) => i.status !== "done");
  const grid = canvasGridClass(items);

  // 逐个看焦点随篇数收敛（定稿/失败恢复后栏数变少）
  useEffect(() => {
    if (focus > openItems.length - 1) setFocus(Math.max(0, openItems.length - 1));
  }, [openItems.length, focus]);

  // 所有 hooks 必须在早退之前
  if (!canvasMode || !grid) return null;

  const reviewItems = openItems.filter((i) => i.status === "needs_review");
  const ocrPending = openItems.some((i) => i.status === "transcribing");

  /** 把已确认的篇一次性发给 agent 逐篇批改（调用方保证已无待核对/转写中） */
  const sendConfirmed = (texts: Record<string, string>) => {
    const essays = items.filter((i) => texts[i.id]).map((i) => ({ title: i.title, text: texts[i.id] }));
    if (essays.length === 0) return;
    // 界面确认动作：正文仍发给模型，但对话区只显示一行灰条，不冒充用户输入
    t.send(buildOcrConfirmMessage(essays), [], undefined, "已核对照片作文转写稿，开始批改");
  };

  /** 单篇确认：齐了就发；逐个看模式自动跳下一张待核对 */
  const handleConfirm = (item: BatchItem, text: string) => {
    confirmOcr(item.id, text);
    const rest = reviewItems.filter((i) => i.id !== item.id);
    if (rest.length === 0 && !ocrPending) {
      sendConfirmed({ ...confirmed, [item.id]: text });
      return;
    }
    if (layout === "single" && rest[0]) setFocus(openItems.indexOf(rest[0]));
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

  const focused = openItems[focus] ?? openItems[0];
  const shown = layout === "single" ? (focused ? [focused] : []) : openItems;
  const colsClass = layout === "single" ? "n1" : grid;
  const activeId = reviewItems[0]?.id;

  const renderBody = (item: BatchItem, index: number) => {
    if (item.status === "needs_decision") {
      return <GradePane item={item} index={index} />;
    }
    if (item.status === "ocr_failed" || item.status === "grade_failed") {
      return <ErrPane item={item} index={index} onRetry={() => t.send(buildRetryMessage(item, index), [])} />;
    }
    if (item.status === "needs_review" || item.status === "review_confirmed") {
      return (
        <OcrPane
          item={item}
          index={index}
          compact={layout === "split" && openItems.length > 1}
          busy={ocrPending || reviewItems.some((i) => i.id !== item.id)}
          onEdit={(txt) => setDraft((d) => ({ ...d, [item.id]: txt }))}
          onConfirm={(text) => handleConfirm(item, text)}
          onUnconfirm={() => {
            unconfirmOcr(item.id);
            setDraft((d) => ({ ...d, [item.id]: item.confirmedText ?? item.transcript ?? "" }));
          }}
        />
      );
    }
    return <StagePlaceholder item={item} />;
  };

  return (
    <div className="canvas-stage">
      <div className="canvas-toolbar">
        <button className="btn ghost sm" onClick={() => setCanvasMode(false)}>返回货架视图</button>
        <span className="muted" style={{ marginLeft: 4 }}>各篇互不等待，谁先好谁先处理</span>
        <span className="sp" />
        {openItems.length > 1 ? (
          <div className="seg">
            <button className={layout === "split" ? "on" : ""} onClick={() => setLayout("split")}>
              并排 {openItems.length} 篇
            </button>
            <button className={layout === "single" ? "on" : ""} onClick={() => setLayout("single")}>
              逐个看
            </button>
          </div>
        ) : null}
        {reviewItems.length > 0 || ocrPending ? (
          <button className="btn primary sm" disabled={ocrPending || reviewItems.length === 0} onClick={handleConfirmAll}>
            全部确认并批改{reviewItems.length > 0 ? `（${reviewItems.length} 张按原转写稿）` : ""}
          </button>
        ) : null}
      </div>

      <div className={"canvas-cols " + colsClass}>
        {shown.map((item) => {
          const index = items.indexOf(item);
          const st = stageInfo(item);
          return (
            <div className={"cc" + (item.id === activeId ? " active" : "")} key={item.id}>
              <div
                className="cc-head"
                onClick={layout === "single" ? undefined : () => { setLayout("single"); setFocus(index); }}
                title={layout === "single" ? undefined : "点开逐个看这篇"}
              >
                <span className="cct">第 {index + 1} 篇 · {item.title}</span>
                <span className="sp" />
                <span className={"stage " + st.kind}>
                  {st.kind === "run" ? <span className="spin sm" style={{ marginRight: 3 }} /> : <span className="sd" />}
                  {st.label}
                </span>
              </div>
              <div className="cc-body">{renderBody(item, index)}</div>
            </div>
          );
        })}
      </div>

      {layout === "single" && openItems.length > 1 ? (
        <div className="canvas-foot">
          <button className="btn ghost sm" disabled={focus === 0} onClick={() => setFocus((f) => f - 1)}>← 上一篇</button>
          <span className="muted">{focus + 1} / {openItems.length}</span>
          <button className="btn ghost sm" disabled={focus === openItems.length - 1} onClick={() => setFocus((f) => f + 1)}>下一篇 →</button>
          <span className="sp" />
          <button className="btn ghost sm" onClick={() => setLayout("split")}>回到并排</button>
        </div>
      ) : null}
    </div>
  );
}
