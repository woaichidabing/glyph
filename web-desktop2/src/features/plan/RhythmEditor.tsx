/* 周节奏编辑：整周模板 + 单日调整（带“只改这周 / 以后都这样”）。
 * 两个弹窗共用一套行编辑：每天一行 chip + 一个“加一条”输入。 */
import { useState } from "react";
import type { PlanRhythm } from "./planApi";

const WEEK = ["一", "二", "三", "四", "五", "六", "日"];

type Draft = Record<string, { t: string }[]>;

function toDraft(rhythm: PlanRhythm): Draft {
  const out: Draft = {};
  for (let i = 1; i <= 7; i += 1) {
    out[String(i)] = (rhythm[String(i)] ?? []).map((x) => ({ t: x.t }));
  }
  return out;
}

function DayRows({
  draft,
  onChange,
}: {
  draft: Draft;
  onChange: (d: Draft) => void;
}) {
  const [typed, setTyped] = useState<Record<string, string>>({});
  return (
    <>
      {WEEK.map((label, idx) => {
        const key = String(idx + 1);
        const items = draft[key] ?? [];
        const add = () => {
          const text = (typed[key] ?? "").trim();
          if (!text) return;
          onChange({ ...draft, [key]: [...items, { t: text }] });
          setTyped((prev) => ({ ...prev, [key]: "" }));
        };
        return (
          <div className="pl-rmed-day" key={key}>
            <div className="pl-rmed-dh">周{label}</div>
            {items.length === 0 ? <p className="pl-rmed-empty">休息（没有任务）</p> : null}
            <div>
              {items.map((item, i) => (
                <span className="pl-rmed-chip" key={i}>
                  {item.t}
                  <button
                    type="button"
                    aria-label="删掉这条"
                    onClick={() => onChange({ ...draft, [key]: items.filter((_, j) => j !== i) })}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            <div className="pl-rmed-add">
              <input
                value={typed[key] ?? ""}
                placeholder="加一条，回车"
                onChange={(e) => setTyped((prev) => ({ ...prev, [key]: e.target.value }))}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { e.preventDefault(); add(); }
                }}
              />
              <button type="button" className="btn" onClick={add}>加</button>
            </div>
          </div>
        );
      })}
    </>
  );
}

export function WeekRhythmModal({
  rhythm,
  onClose,
  onSave,
}: {
  rhythm: PlanRhythm;
  onClose: () => void;
  onSave: (rhythm: PlanRhythm) => Promise<void>;
}) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(rhythm));
  const [busy, setBusy] = useState(false);
  return (
    <div
      className="pl-modal-backdrop"
      role="presentation"
      onClick={(ev) => { if (ev.target === ev.currentTarget) onClose(); }}
    >
      <div className="pl-modal wide" role="dialog" aria-modal="true" aria-labelledby="pl-week-title">
        <h3 id="pl-week-title">调整周节奏</h3>
        <p className="pl-rmed-hint">这里改的是“每周都这样”。想只改某一天，去那天的列表里改。</p>
        <div className="pl-rmed-body"><DayRows draft={draft} onChange={setDraft} /></div>
        <div className="pl-modal-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
          <button
            type="button"
            className="btn primary"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              const next: PlanRhythm = {};
              for (const key of Object.keys(draft)) {
                if (draft[key].length) next[key] = draft[key].map((x) => ({ t: x.t }));
              }
              void onSave(next).finally(() => setBusy(false));
            }}
          >
            {busy ? "正在保存…" : "保存"}
          </button>
        </div>
      </div>
    </div>
  );
}

export function DayEditModal({
  title,
  initial,
  onClose,
  onSave,
}: {
  title: string;
  initial: string[];
  onClose: () => void;
  onSave: (items: { t: string }[], scope: "day" | "weekly") => Promise<void>;
}) {
  const [items, setItems] = useState<string[]>(initial);
  const [typed, setTyped] = useState("");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);

  const add = () => {
    const text = typed.trim();
    if (!text) return;
    setItems((prev) => [...prev, text]);
    setTyped("");
  };

  return (
    <div
      className="pl-modal-backdrop"
      role="presentation"
      onClick={(ev) => { if (ev.target === ev.currentTarget && !asking) onClose(); }}
    >
      <div className="pl-modal" role="dialog" aria-modal="true" aria-labelledby="pl-dayedit-title">
        <h3 id="pl-dayedit-title">{title}的安排</h3>
        {asking ? (
          <>
            <p className="pl-rmed-hint">这次改动，以后每个这天都这样吗？</p>
            <div className="pl-rmed-choice">
              <button
                type="button"
                className="pl-rmed-opt"
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  void onSave(items.map((t) => ({ t })), "day").finally(() => setBusy(false));
                }}
              >
                <b>只改这周</b><span>只动这一天，下次还是原来的安排。</span>
              </button>
              <button
                type="button"
                className="pl-rmed-opt"
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  void onSave(items.map((t) => ({ t })), "weekly").finally(() => setBusy(false));
                }}
              >
                <b>以后都这样</b><span>把“每周这天”的模板也改掉，下周起照新的来。</span>
              </button>
            </div>
            <p className="pl-rmed-hint">选“以后都这样”时：今天只补新加的；明天到周日没做的按新模板重建，做完的保留。</p>
            <div className="pl-modal-actions">
              <button type="button" className="btn" onClick={() => setAsking(false)} disabled={busy}>返回修改</button>
            </div>
          </>
        ) : (
          <>
            {items.length === 0 ? <p className="pl-rmed-empty">这一天没有任务（休息）。</p> : null}
            <div className="pl-rmed-body">
              {items.map((t, i) => (
                <span className="pl-rmed-chip" key={i}>
                  {t}
                  <button type="button" aria-label="删掉这条" onClick={() => setItems((prev) => prev.filter((_, j) => j !== i))}>×</button>
                </span>
              ))}
            </div>
            <div className="pl-rmed-add">
              <input
                value={typed}
                placeholder="加一条，回车"
                onChange={(e) => setTyped(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
              />
              <button type="button" className="btn" onClick={add}>加</button>
            </div>
            <div className="pl-modal-actions">
              <button type="button" className="btn" onClick={onClose}>取消</button>
              <button type="button" className="btn primary" onClick={() => setAsking(true)}>保存</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
