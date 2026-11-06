import { useState } from "react";
import type { FinalizeNode } from "../adapters/askTimeline";
import { renderFinalText, type MarkSeg } from "../adapters/finalize";
import { useStore } from "../store";

/** 讲解清单拼纯文本（复制讲解用） */
function explanationsToText(n: FinalizeNode): string {
  const lines = (n.explanations ?? [])
    .filter((e) => e.explanationZh)
    .map((e, i) => {
      const change = e.corrected != null && e.original
        ? `${e.original} → ${e.corrected}：`
        : "";
      return `${i + 1}. ${change}${e.explanationZh ?? ""}`;
    });
  return [`《${n.title ?? "定稿"}》讲解清单`, ...lines].join("\n");
}

/**
 * 定稿卡（对话流内的成果出口）：绿色头 + 定稿全文（改后片段绿色高亮）
 * + 折叠讲解清单 + 复制全文 / 复制讲解 / 我再改两句。不做文件导出。
 * 历史回放的老定稿没有 marks / 讲解，退化为纯文本全文。
 */
export default function FinalizeCard({ node }: { node: FinalizeNode }) {
  const setAskDraft = useStore(s => s.setAskDraft);
  const [copied, setCopied] = useState<"" | "all" | "exp">("");

  const title = node.title ?? "本次定稿";
  const html = renderFinalText(node.finalText, node.marks as MarkSeg[] | undefined);
  const hasExp = (node.explanations ?? []).some((e) => e.explanationZh);

  const copy = async (kind: "all" | "exp", text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 非安全上下文（极少数情况）兜底：临时 textarea + execCommand
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* 复制不了就静默 */ }
      ta.remove();
    }
    setCopied(kind);
    setTimeout(() => setCopied(""), 1600);
  };

  return (
    <div className="dd-card">
      <div className="dd-head">
        <span className="dd-ic" aria-hidden>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 15l2 2 4-4"/></svg>
        </span>
        <span>{title}</span>
        <span className="dd-tag">已定稿 · 采纳 {node.keptCount} 处</span>
      </div>

      <div className="dd-body">
        <h3>{title}</h3>
        <p dangerouslySetInnerHTML={{ __html: html }} />
      </div>

      {hasExp ? (
        <details className="dd-exp">
          <summary>讲解清单（{node.keptCount} 处）▾</summary>
          {node.explanations!.filter((e) => e.explanationZh).map((e, i) => (
            <div className="xr" key={i}>
              <b>{i + 1}.</b>
              <span>
                {e.original && e.corrected != null
                  ? <><del>{e.original}</del> → <ins>{e.corrected}</ins>：</>
                  : null}
                {e.explanationZh}
              </span>
            </div>
          ))}
        </details>
      ) : null}

      <div className="dd-actions">
        <button className="btn sm" onClick={() => copy("all", node.finalText)}>
          {copied === "all" ? "已复制 ✓" : "复制全文"}
        </button>
        {hasExp ? (
          <button className="btn sm" onClick={() => copy("exp", explanationsToText(node))}>
            {copied === "exp" ? "已复制 ✓" : "复制讲解"}
          </button>
        ) : null}
        <button className="btn ghost sm" onClick={() => setAskDraft(
          `在《${title}》这篇定稿的基础上，我还想再改两句：____`,
        )}>我再改两句</button>
      </div>
    </div>
  );
}
