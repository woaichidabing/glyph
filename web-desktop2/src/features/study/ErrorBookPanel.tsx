import { useEffect, useRef, useState } from "react";
import type { DictEntry, ErrorWord } from "../../types";
import { lookup } from "../../adapters/dictAdapter";
import { useStudy } from "./studyStore";
import { Pronounce } from "../../components/Pronounce";

/** 三级掀开：0 只给词（可听音）→ 1 框变色（标记真不会）→ 2 出音标释义，再点回 0 */
type Level = 0 | 1 | 2;
const PAGE = 50;

/**
 * 错词本面板（从背词页头部进入）。
 * 不是用来背的，是快速过一遍、认出哪些真不会：点一下框变色，再点一下出释义。
 * 旧版「出一张卡」依赖未移植的 cardRepo，本次不带（不做无功能按钮）。
 */
export function ErrorBookPanel({ onBack, onStudyWrong }: {
  onBack: () => void;
  onStudyWrong: () => void;
}) {
  const errors = useStudy((s) => s.errors);
  const dropError = useStudy((s) => s.dropError);
  const clearErrors = useStudy((s) => s.clearErrors);
  const restoreError = useStudy((s) => s.restoreError);

  const [levels, setLevels] = useState<Record<string, Level>>({});
  const [revealAll, setRevealAll] = useState(false);
  const [visible, setVisible] = useState(PAGE);
  const [entries, setEntries] = useState<Record<string, DictEntry>>({});
  const [lastRemoved, setLastRemoved] = useState<ErrorWord | null>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  /** 清空确认弹窗（浏览器原生 confirm 在这个环境会被拦，用应用内弹窗） */
  const [clearOpen, setClearOpen] = useState(false);

  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (es) => { if (es[0].isIntersecting) setVisible((v) => Math.min(v + PAGE, errors.length)); },
      { rootMargin: "200px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [errors.length]);

  const need = errors
    .slice(0, visible)
    .filter((e) => (revealAll ? 2 : levels[e.word] ?? 0) === 2)
    .map((e) => e.word)
    .filter((w) => !entries[w.toLowerCase()]);

  useEffect(() => {
    if (need.length === 0) return;
    let alive = true;
    Promise.all(need.map((w) => lookup(w).catch(() => null))).then((list) => {
      if (!alive) return;
      setEntries((m) => {
        const next = { ...m };
        list.forEach((e) => { if (e) next[e.word.toLowerCase()] = e; });
        return next;
      });
    });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [need.join("|"), revealAll, visible]);

  const bump = (word: string) => setLevels((m) => {
    const cur = m[word] ?? 0;
    return { ...m, [word]: cur === 0 ? 1 : cur === 1 ? 2 : 0 };
  });

  if (errors.length === 0) {
    return (
      <div className="nb-wrap">
        <div className="dict-state">
          <b>错词本是空的</b>
          <span>背词时反复答错的词会自动落到这里，方便你集中过一遍。</span>
          <div className="dict-state-actions"><button type="button" className="btn primary" onClick={onBack}>回背词页</button></div>
        </div>
      </div>
    );
  }

  return (
    <div className="nb-wrap">
      <header className="nb-head">
        <div>
          <button type="button" className="sc-link" onClick={onBack}>← 回背词页</button>
          <h2 className="nb-title">这些词，你总是记不住</h2>
          <p className="nb-sub">点一下框变色（标记真不会），再点一下出音标释义，再点收回。</p>
        </div>
        <div className="nb-tools">
          <button type="button" className={"pb-chip" + (revealAll ? " on" : "")}
            onClick={() => setRevealAll((v) => !v)} aria-pressed={revealAll}>
            {revealAll ? "收回全部" : "一眼看全"}
          </button>
          <button type="button" className="pb-chip" onClick={() => setClearOpen(true)}>
            清空
          </button>
        </div>
      </header>

      <div className="eb-list">
        {errors.slice(0, visible).map((e) => {
          const level: Level = revealAll ? 2 : (levels[e.word] ?? 0);
          const entry = entries[e.word.toLowerCase()];
          return (
            <div key={e.word} className={"eb-card" + (level === 1 ? " warn" : "") + (level === 2 ? " open" : "")}>
              <div role="button" tabIndex={0} className="eb-main"
                onClick={() => bump(e.word)}
                onKeyDown={(ev) => {
                  if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); bump(e.word); }
                }}>
                <div className="eb-wordline">
                  <span className="nb-word">{e.word}</span>
                  <span className="sc-hint">错 {e.lapses} 次 · 连对 {e.goodStreak}</span>
                </div>
                <div onClick={(ev) => ev.stopPropagation()} role="presentation" className="eb-sound">
                  <Pronounce word={e.word} phonetic={entry?.phonetic} hidePhonetic single />
                </div>

                {level === 2 ? (
                  <div className="eb-detail" onClick={(ev) => ev.stopPropagation()} role="presentation">
                    {entry ? (
                      <>
                        <div className="nb-phn">{entry.phonetic.us || entry.phonetic.uk || "—"}</div>
                        <div className="nb-gloss">{entry.senses[0]?.translation ?? "—"}</div>
                        {entry.senses[0]?.examples[0] ? (
                          <div className="e-ex">
                            <p className="ex-en">{entry.senses[0].examples[0].en}</p>
                            <p className="ex-cn">{entry.senses[0].examples[0].zh}</p>
                          </div>
                        ) : null}
                      </>
                    ) : (
                      <span className="sc-hint">释义取中……</span>
                    )}

                    {e.from || e.sentence || e.reason ? (
                      <div className="eb-src">
                        <span className="sc-hint">出处</span>
                        {e.from ? <div className="eb-src-line">{e.from}</div> : null}
                        {e.sentence ? <p className="ex-en">{e.sentence}</p> : null}
                        {e.reason ? <div className="eb-src-reason">{e.reason}</div> : null}
                      </div>
                    ) : null}

                    <div className="eb-actions">
                      <button type="button" className="sc-link" onClick={onStudyWrong}>加入今日复习 →</button>
                      <button type="button" className="sc-link"
                        onClick={() => { dropError(e.word); setLastRemoved(e); }}>
                        移出错词本
                      </button>
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      <div ref={sentinel} className="h-8" />

      {clearOpen ? (
        <div
          className="eb-modal-backdrop"
          role="presentation"
          onClick={(ev) => { if (ev.target === ev.currentTarget) setClearOpen(false); }}
        >
          <div
            className="eb-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="eb-clear-title"
            onKeyDown={(ev) => { if (ev.key === "Escape") setClearOpen(false); }}
            tabIndex={-1}
            ref={(el) => el?.focus()}
          >
            <h3 id="eb-clear-title" className="eb-modal-title">清空这 {errors.length} 条？</h3>
            <p className="eb-modal-body">只清这个列表，背词进度和卡片状态不动。</p>
            <p className="eb-modal-hint">清空后不能撤销。</p>
            <div className="eb-modal-actions">
              <button type="button" className="btn sm eb-ghost" onClick={() => setClearOpen(false)}>
                取消
              </button>
              <button
                type="button"
                className="btn sm eb-danger"
                onClick={() => { clearErrors(); setClearOpen(false); }}
              >
                清空
              </button>
            </div>
          </div>
        </div>
      ) : null}
      <p className="sc-hint" style={{ textAlign: "center" }}>
        共 {errors.length} 词 · 已显示 {Math.min(visible, errors.length)} 词
      </p>

      {lastRemoved ? (
        <div className="nb-undo">
          <span>已移出 {lastRemoved.word}</span>
          <button type="button" className="btn primary sm"
            onClick={() => { restoreError(lastRemoved); setLastRemoved(null); }}>撤销</button>
        </div>
      ) : null}
    </div>
  );
}
