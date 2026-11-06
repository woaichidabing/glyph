/**
 * 「上下文用量」面板：点发送区那颗环打开。
 *
 * 版式照用户给的参考图（2026-10-12）：标签左、数值右对齐、标题行下面跟进度条、
 * 分区之间一条细线；不打彩色圆点，占比用堆叠条。**没有**"额度用量"那种区块
 * （本产品没有额度概念），也**没有**"固定前导"那行（用户明确去掉）。
 *
 * 两个数字来源不同，界面不硬凑：
 * - 总量用**模型实测**（上一轮真实发出去的输入）；
 * - 分类用后端对本地字节的**保守估算**，按比例缩放到实测总量上。
 * 因为分类是估的，标题上写的就是"≈"的语义（一位小数的 K/M 写法本身也说明它不是精确值）。
 */
import { useEffect, useMemo, useState } from "react";
import {
  buildContextView,
  loadContextParts,
  type ContextPartsPayload,
} from "../../adapters/contextParts";
import { friendlyTokens } from "../../adapters/usage";

export default function ContextInspector({
  usedTokens,
  windowTokens,
}: {
  usedTokens: number | null;
  windowTokens: number;
}) {
  const [data, setData] = useState<ContextPartsPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    loadContextParts()
      .then((d) => {
        if (alive) setData(d);
      })
      .catch((e: Error) => {
        if (alive) setErr(e.message);
      });
    return () => {
      alive = false;
    };
  }, []);

  const view = useMemo(() => (data ? buildContextView(data, usedTokens) : null), [data, usedTokens]);

  if (err) return <span className="bp-error" role="alert">{err}</span>;
  if (!view) return <span className="ctx-loading">正在量…</span>;

  const pct = windowTokens > 0 ? Math.min(100, (view.total / windowTokens) * 100) : 0;

  return (
    <div className="ctx-panel">
      <div className="ctx-h">
        <span className="ctx-lb">上下文用量</span>
        <span className="ctx-vl">
          <b>{friendlyTokens(view.total)}</b>/{friendlyTokens(windowTokens)}
        </span>
        <span className="ctx-pct">{pct.toFixed(1)}%</span>
        <span className="ctx-cv" aria-hidden>›</span>
      </div>
      <div className="ctx-bar" aria-hidden>
        <i className="ctx-top" style={{ width: `${pct}%` }} />
      </div>

      <div className="ctx-div" />

      <div className="ctx-h">
        <span className="ctx-lb">构成</span>
      </div>
      <div className="ctx-bar" aria-hidden>
        {view.slices.map((s) => (
          <i key={s.key} className={`t${s.tone}`} style={{ width: `${s.pct}%` }} />
        ))}
      </div>
      {view.slices.map((s) => (
        // 「对话」不加粗：前两块是每轮都发的（可砍），它是每轮都在变的，视觉上让位
        <div className={`ctx-row${s.key === "talk" ? "" : " cut"}`} key={s.key}>
          <i className={`ctx-sw t${s.tone}`} aria-hidden />
          <span className="ctx-name">{s.label}</span>
          <b>{friendlyTokens(s.tokens)}</b>
        </div>
      ))}
      {view.slices.length === 0 ? <div className="ctx-row ctx-empty">还没有可量的内容</div> : null}
    </div>
  );
}
