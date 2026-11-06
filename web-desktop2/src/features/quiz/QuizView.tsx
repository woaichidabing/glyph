import { useCallback, useEffect, useMemo, useState } from "react";
import { useStore } from "../../store";
import {
  countsChips,
  deletePapers,
  groupPapers,
  listPapers,
  paperTitle,
  type PaperSummary,
} from "./quizLibrary";
import ImportPapersModal from "./ImportPapersModal";

const DOC_ICON = (
  <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
);
const HERO_ICON = (
  <svg width="58" height="58" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="m9 14 2 2 4-4"/></svg>
);
const TRASH_ICON = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/></svg>
);
const PLUS_ICON = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
);

function importDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日导入`;
}

type LoadState = "loading" | "ok" | "error";
type Confirm = { keys: string[]; cards: number } | null;

export default function QuizView({ hidden }: { hidden: boolean }) {
  const setView = useStore(s => s.setView);
  const setAskDraft = useStore(s => s.setAskDraft);

  const [state, setState] = useState<LoadState>("loading");
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [errorMsg, setErrorMsg] = useState("");
  const [notice, setNotice] = useState("");

  const [modalOpen, setModalOpen] = useState(false);
  const [managing, setManaging] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async () => {
    setState("loading");
    const out = await listPapers();
    if (out.ok) {
      setPapers(out.papers);
      setState("ok");
    } else {
      setErrorMsg(out.error);
      setState("error");
    }
  }, []);

  useEffect(() => {
    if (!hidden) load();
  }, [hidden, load]);

  const groups = useMemo(() => groupPapers(papers), [papers]);
  const deletable = useMemo(() => papers.filter(p => !p.builtin), [papers]);

  const togglePick = (key: string) => setSelected(prev => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const selectAll = () => setSelected(new Set(deletable.map(p => p.key)));
  const clearPick = () => setSelected(new Set());

  const cardsOf = (keys: string[]) =>
    papers.filter(p => keys.includes(p.key)).reduce((sum, p) => sum + p.card_count, 0);

  const askDelete = (keys: string[]) => {
    const userKeys = keys.filter(k => papers.some(p => p.key === k && !p.builtin));
    if (userKeys.length === 0) return;
    setConfirm({ keys: userKeys, cards: cardsOf(userKeys) });
  };

  const doDelete = async () => {
    if (!confirm || deleting) return;
    setDeleting(true);
    const out = await deletePapers(confirm.keys);
    setDeleting(false);
    if (!out.ok) {
      setNotice(out.error);
      setConfirm(null);
      return;
    }
    setConfirm(null);
    setManaging(false);
    setSelected(new Set());
    if (out.denied.length || out.missing.length) {
      setNotice(out.message || "有几套没删成（内置题卡不可删）。");
    }
    await load();
  };

  const goPractice = (prompt: string) => {
    setModalOpen(false);
    setAskDraft(prompt);
    setView("ask");
  };

  const isEmpty = state === "ok" && papers.length === 0;

  return (
    <section className="view" id="view-library" hidden={hidden}>
      <div className="qb-page">
        {state === "loading" ? (
          <div className="qb-status"><span className="spin" />正在打开题库…</div>
        ) : state === "error" ? (
          <div className="qb-status">
            <p className="qb-err">{errorMsg}</p>
            <button type="button" className="btn ghost sm" onClick={load}>重试</button>
          </div>
        ) : isEmpty ? (
          /* 状态 1：空态 */
          <div className="qb-empty">
            <div className="qb-hero">{HERO_ICON}</div>
            <h2>把真题装进 Glyph</h2>
            <p className="qb-lead">
              把历年 <b>四级 / 六级 / 考研 / 高考</b> 真题（连同 <b>标准答案</b>）拖进来，Glyph
              在本机自动拆成题卡。之后在对话里说一声「来道四级仔细阅读」就能练，判卷用的是你给的标准答案。
            </p>
            <button type="button" className="btn primary lg qb-cta" onClick={() => setModalOpen(true)}>
              {PLUS_ICON}导入真题
            </button>
            <div className="qb-meta">
              <span>支持 PDF / Word(.docx) / txt</span>
              <span>题目和答案在同一份里也行</span>
              <span>本机处理，不上传、不花 token</span>
            </div>
          </div>
        ) : (
          <>
            {/* 状态 7 / 10：列表 + 管理 */}
            <div className="qb-head">
              <div>
                <h2>题库</h2>
                <div className="qb-hint">练的是你导入的真题，判卷用卷子里的标准答案 · 全部只存在本机</div>
              </div>
              <div className="qb-head-actions">
                <button type="button" className="btn primary sm" onClick={() => setView("reading")}>去精读台练习</button>
                {deletable.length > 0 ? (
                  managing ? (
                    <button type="button" className="btn ghost sm" onClick={() => { setManaging(false); clearPick(); }}>取消选择</button>
                  ) : (
                    <button type="button" className="btn ghost sm" onClick={() => setManaging(true)}>选择</button>
                  )
                ) : null}
                <button type="button" className="btn primary sm" onClick={() => setModalOpen(true)}>{PLUS_ICON}导入真题</button>
              </div>
            </div>

            {managing ? (
              <div className="qb-managebar">
                <span className="qb-mbcount">已选 {selected.size} 套</span>
                <button type="button" className="qb-mblink" onClick={selectAll}>全选</button>
                <button type="button" className="qb-mblink" onClick={clearPick}>清空</button>
                <span className="qb-mbsp" />
                <button type="button" className="btn danger sm" disabled={selected.size === 0}
                  onClick={() => askDelete([...selected])}>删除 ({selected.size})</button>
              </div>
            ) : null}

            {notice ? <p className="set-msg err" role="status">{notice}</p> : null}

            {groups.map(g => (
              <div className="qb-group" key={g.title}>
                <div className="qb-grouph">{g.title}</div>
                {g.papers.map(p => {
                  const chips = countsChips(p);
                  const date = importDate(p.updated_at);
                  const picked = selected.has(p.key);
                  return (
                    <div
                      key={p.key}
                      className={"qb-card" + (managing && !p.builtin ? " pickable" : "") + (picked ? " picked" : "")}
                      onClick={managing && !p.builtin ? () => togglePick(p.key) : undefined}>
                      {managing && !p.builtin ? (
                        <span className="qb-ck" aria-hidden>{picked ? (
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 5 5L20 7"/></svg>
                        ) : null}</span>
                      ) : null}
                      <span className="qb-cardic">{DOC_ICON}</span>
                      <div className="qb-cardmain">
                        <div className="qb-t">{paperTitle(p)}</div>
                        <div className="qb-sub">
                          {chips.map(c => (
                            <span key={c.label}>{c.label} {c.n} 篇<i>·</i></span>
                          ))}
                          {date ? <span>{date}</span> : null}
                        </div>
                      </div>
                      <span className="qb-badgewrap">
                        {p.builtin ? <span className="qb-tag">内置</span> : null}
                        <span className="qb-badge">{p.card_count} 篇题卡</span>
                      </span>
                      {!managing && !p.builtin ? (
                        <button type="button" className="qb-trash" title="删除这一套"
                          onClick={e => { e.stopPropagation(); askDelete([p.key]); }}>{TRASH_ICON}</button>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ))}
          </>
        )}
      </div>

      <ImportPapersModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onImported={load}
        onPractice={goPractice}
      />

      {/* 删除确认 */}
      {confirm ? (
        <div className="set-mask open" onMouseDown={e => { if (e.target === e.currentTarget && !deleting) setConfirm(null); }}>
          <div className="set-modal qb-confirm" role="dialog" aria-label="确认删除">
            <div className="set-body">
              <div className="qb-confirmrow">
                <span className="qb-confirmic">{TRASH_ICON}</span>
                <div>
                  <h4>删除选中的 {confirm.keys.length} 套？</h4>
                  <p className="qi-esub">
                    将同时删除它们包含的 <b>{confirm.cards} 篇题卡</b>（仔细阅读 / 段落匹配 / 选词填空 / 写作）。
                    题卡只存在你的电脑上，删除后无法恢复。
                  </p>
                </div>
              </div>
            </div>
            <div className="su-foot">
              <span style={{ flex: 1 }} />
              <button type="button" className="btn ghost sm" disabled={deleting} onClick={() => setConfirm(null)}>取消</button>
              <button type="button" className="btn danger sm" disabled={deleting} onClick={doDelete}>
                {deleting ? <><span className="spin sm" />删除中…</> : "删除"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
