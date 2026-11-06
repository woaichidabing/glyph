/* ============================================================
   ask_user 决策卡（方案 A · Codex 风决策卡）
   模型在关键岔路停下来问一句：问题 + 2~4 个选项 + 自定义输入。
   - waiting：选项大按钮（推荐项高亮）、数字键快选、自己打字展开输入框
   - submitting：答案已送，按钮禁用转圈，等同一条流的 tool_result 收口
   - answered：收成一行摘要（不丢上下文），可展开回看原问题
   - dismissed：不渲染（steer/取消/迟到由时间线状态机处理）
   ============================================================ */
import { useEffect, useRef, useState } from "react";
import type { AskNode } from "../../adapters/askTimeline";

export default function AskCard({
  node,
  onAnswer,
}: {
  node: AskNode;
  onAnswer: (callId: string, choice: string | null, text: string) => void;
}) {
  const [customOpen, setCustomOpen] = useState(false);
  const [customText, setCustomText] = useState("");
  const [peek, setPeek] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const submitting = !!node.submitting;

  // 数字键 1~4 快选（焦点在自定义输入框里时不抢）
  useEffect(() => {
    if (node.state !== "waiting" || submitting) return;
    const onKey = (e: KeyboardEvent) => {
      if (document.activeElement === inputRef.current) return;
      const n = Number(e.key);
      if (n >= 1 && n <= node.options.length) {
        e.preventDefault();
        onAnswer(node.callId, node.options[n - 1].label, "");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [node, submitting, onAnswer]);

  useEffect(() => {
    if (customOpen) inputRef.current?.focus();
  }, [customOpen]);

  const sendCustom = () => {
    const text = customText.trim();
    if (!text || submitting) return;
    onAnswer(node.callId, null, text);
  };

  /* 已答：收合行，可展开回看 */
  if (node.state === "answered") {
    const summary =
      node.answerKind === "custom"
        ? `按你说的「${node.answer ?? ""}」继续`
        : `已按「${node.answer ?? ""}」继续`;
    return (
      <div className="au-card answered">
        <div className="au-done">
          <span className="ck">✓</span>
          <span>{summary}</span>
          <button className="peek" onClick={() => setPeek(v => !v)}>
            {peek ? "收起" : "查看原问题"}
          </button>
        </div>
        {peek ? (
          <div className="au-peek">
            <div className="au-q">{node.question}</div>
            <div className="au-opts au-opts-static">
              {node.options.map(o => (
                <div key={o.label} className={`au-opt ${o.rec ? "rec" : ""}`}>
                  <span className="lab">
                    {o.label}{o.rec ? <span className="au-badge">推荐</span> : null}
                  </span>
                  {o.description ? <span className="desc">{o.description}</span> : null}
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    );
  }

  /* waiting / submitting */
  return (
    <div className="au-card">
      <div className="au-head">
        <span className="live" />
        <span className="tag">Glyph 需要你定一下</span>
        <span className="wait">
          {submitting ? <><span className="spin sm" />提交中…</>
            : <>等你选择后继续</>}
        </span>
      </div>
      <div className="au-q">{node.question}</div>
      <div className="au-opts">
        {node.options.map((o, i) => (
          <button
            key={o.label}
            type="button"
            className={`au-opt ${o.rec ? "rec" : ""}`}
            disabled={submitting}
            onClick={() => onAnswer(node.callId, o.label, "")}
          >
            <span className="lab">
              {o.label}
              {o.rec ? <span className="au-badge">推荐</span> : null}
            </span>
            {o.description ? <span className="desc">{o.description}</span> : null}
            <span className="kbd" aria-hidden>{i + 1}</span>
          </button>
        ))}
      </div>
      <div className="au-foot">
        <button
          type="button"
          className="au-custom"
          disabled={submitting}
          onClick={() => setCustomOpen(v => !v)}
        >
          ✎ {customOpen ? "收起" : "我自己回答"}
        </button>
        {customOpen ? (
          <div className="au-customrow">
            <input
              ref={inputRef}
              value={customText}
              disabled={submitting}
              placeholder="直接说你的情况或要求，回车发送"
              onChange={e => setCustomText(e.target.value)}
              onKeyDown={e => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  sendCustom();
                }
              }}
            />
            <button
              type="button"
              className="go"
              disabled={submitting || !customText.trim()}
              onClick={sendCustom}
            >
              发送
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
