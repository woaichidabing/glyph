/**
 * 设置 · 用量与花费：把"这个月花了多少、省了多少"摆到台面上。
 *
 * 与对话页那条状态条同一套口径（`adapters/usageStats.ts`）：命中缓存的部分按标准价
 * 2 折计费，所以"省了多少"是按钱算的；命中率的分母只认带了缓存明细的那部分输入
 * （2026-10-12 之前的流水没记这个字段，混进去会把 95% 压成 19%）。
 */
import { useEffect, useState } from "react";
import {
  bars,
  hitPct,
  loadUsageSummary,
  savedPct,
  type UsageSummaryPayload,
} from "../../adapters/usageStats";
import { friendlyTokens } from "../../adapters/usage";

export default function UsagePane() {
  const [data, setData] = useState<UsageSummaryPayload | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    loadUsageSummary(7)
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

  if (err) return <div className="set-msg err">{err}</div>;
  if (!data) return <div className="set-msg">正在读取用量…</div>;

  const total = data.total;
  const saved = savedPct(total);
  const hit = hitPct(total);
  const shape = bars(data.days);

  return (
    <>
      <div className="us-cards">
        <div className="us-card">
          <div className="us-k">近 7 天输入</div>
          <div className="us-v">{friendlyTokens(total.input_tokens)}</div>
          <div className="us-s">
            {saved === null ? "这批流水没记缓存明细" : `省了 ${saved}%（缓存命中 ${hit}%）`}
          </div>
        </div>
        <div className="us-card">
          <div className="us-k">近 7 天输出</div>
          <div className="us-v">{friendlyTokens(total.output_tokens)}</div>
          <div className="us-s">含模型的思考字数</div>
        </div>
        <div className="us-card">
          <div className="us-k">对话轮数</div>
          <div className="us-v">{total.turns}</div>
          <div className="us-s">近 7 天一共回答了多少轮</div>
        </div>
      </div>

      <div className="us-chart">
        <div className="us-bars">
          {shape.map((b) => (
            <span key={b.day} className="us-bar" title={`${b.day}`}>
              <i className="us-bar-total" style={{ height: `${b.totalPct}%` }} />
              <i className="us-bar-cached" style={{ height: `${b.cachedPct}%` }} />
            </span>
          ))}
        </div>
        <div className="us-axis">
          {shape.map((b) => (
            <em key={b.day}>{b.label}</em>
          ))}
        </div>
      </div>

      <p className="us-note">
        柱子深色那段是命中缓存的部分——平台按标准价 2 折计费，所以"省了多少"是按钱算的。
        命中率只看记了缓存明细的那些轮，老流水的数字不会把比例压低。
      </p>
    </>
  );
}
