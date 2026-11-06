import { useEffect, useState } from "react";
import type { BatchItem } from "../adapters/batch";

function wordCount(s: string): number {
  const t = s.trim();
  return t === "" ? 0 : t.split(/\s+/).length;
}

/**
 * OCR 核对视图（右翼）：左原图、右可编辑转写稿，[?] 是没看清的位置，直接在稿子里改。
 * 确认闸：这里只负责记录/撤回确认，「什么时候真正发给 agent 批改」由 BatchBoard 统一决定
 * （必须等所有照片转写完、核对完，一次请求逐篇批改，避免互相掐断）。
 */
export default function OcrPane({
  item, index, busy, onConfirm, onUnconfirm, onEdit, compact = false,
}: {
  item: BatchItem;
  index: number;
  /** 还有照片在转写/排队，确认了也得等 */
  busy: boolean;
  onConfirm: (text: string) => void;
  onUnconfirm: () => void;
  /** 编辑内容实时回传，批量确认时父组件拿得到未点确认的那篇稿子 */
  onEdit?: (text: string) => void;
  /** 放大画布并排窄栏：上图下文堆叠（默认左右双栏） */
  compact?: boolean;
}) {
  const confirmed = item.status === "review_confirmed";
  const [text, setText] = useState(item.confirmedText ?? item.transcript ?? "");
  // 切换到另一篇时，用那篇的稿子重置编辑区
  useEffect(() => {
    setText(item.confirmedText ?? item.transcript ?? "");
  }, [item.id, item.confirmedText, item.transcript]);

  const n = item.lowConfidence?.length ?? 0;
  const empty = text.trim() === "";

  return (
    <div className="pane ocr-pane-wrap" style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      <div className={"ocr-split" + (compact ? " compact" : "")}>
        {/* 左：原图 */}
        <div className="ocr-pane">
          <div className="op-head"><b>原图 · 第 {index + 1} 篇</b><span className="sp" /><span className="muted">{item.title}</span></div>
          <div className="ocr-photo-wrap">
            {item.url
              ? <img className="photo" src={item.url} alt={item.title} />
              : <div className="photo photo-empty">原图在历史记录里不可用</div>}
          </div>
        </div>

        {/* 右：转写稿 */}
        <div className="ocr-pane">
          <div className="op-head"><b>转写稿</b><span className="sp" />
            <span className="muted">{wordCount(text)} 词 · 只抄写，不会替你改错误</span>
          </div>
          {n > 0 && !confirmed ? (
            <div className="ocr-warn">
              {n} 处没看清（文稿里的 <span className="conf">[?]</span>）：对照左边照片，把 <span className="conf">[?]</span> 改成正确的词；其余地方抄错了也可以直接改。
            </div>
          ) : null}
          <textarea
            className="ocr-text"
            value={text}
            readOnly={confirmed}
            onChange={(e) => { setText(e.target.value); onEdit?.(e.target.value); }}
            spellCheck={false}
            aria-label="转写稿，可编辑"
          />
          <div className="ocr-foot">
            {confirmed ? (
              <>
                <span className="ocr-ok">✓ 已确认{busy ? "，等其余照片转写核对完一起批改" : "，准备批改"}</span>
                <span className="sp" />
                <button className="btn ghost sm" onClick={onUnconfirm}>改一下</button>
              </>
            ) : (
              <>
                <span className="muted tiny">确认后才会批改，没确认的照片会一直等你</span>
                <span className="sp" />
                <button
                  className="btn primary sm"
                  disabled={empty}
                  onClick={() => onConfirm(text.trim())}
                >
                  {busy ? "确认，等齐再批" : "确认并批改"}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
