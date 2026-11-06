import { useEffect, useMemo, useState } from "react";
import type { DictEntry, Wordbook } from "../../types";
import { LOCAL_BOOKS, getBooks, syncBooks, ensureWords, previewWords, bookWordsSource, bookGloss } from "./wordbooks";
import type { BookEntry } from "../../adapters/wordbookApi";
import { learnedCount } from "./studyRepo";
import { lookup } from "../../adapters/dictAdapter";
import { Pronounce } from "../../components/Pronounce";

const GROUPS = ["全部", "升学", "大学", "出国"];

function fmtN(n: number): string {
  return n.toLocaleString("en-US");
}

function BookRow({ b, selected, onSelect }: { b: Wordbook; selected: boolean; onSelect: () => void }) {
  const learned = learnedCount(b.id);
  return (
    <button type="button" className={"stm-book" + (selected ? " on" : "")} onClick={onSelect} aria-pressed={selected}>
      <div className="stm-book-main">
        <b>{b.name}</b>
        <span>{b.group} · {b.total > 0 ? fmtN(b.total) + " 词" : "词库准备中"}</span>
      </div>
      <span className="stm-book-num">{learned > 0 ? "已学 " + fmtN(learned) : "未开始"}</span>
      <span className="stm-book-diff">难度 {b.difficulty}/5</span>
    </button>
  );
}

function TrialRow({ w, i, gloss }: { w: string; i: number; gloss?: BookEntry }) {
  const [open, setOpen] = useState(false);
  const [entry, setEntry] = useState<DictEntry | null>(null);
  const [tried, setTried] = useState(false);

  useEffect(() => {
    if (!open || tried || gloss) return;
    setTried(true);
    lookup(w).then(setEntry).catch(() => undefined);
  }, [open, tried, w, gloss]);

  const pos = gloss?.pos ?? entry?.senses[0]?.pos;
  const text = gloss?.gloss ?? entry?.senses[0]?.translation;
  return (
    <div className="pb-trial-row">
      <span className="pb-trial-idx">{i + 1}</span>
      <button type="button" className="pb-trial-word" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span>{w}</span>
        {open ? (
          text ? <em>{pos ? pos + " " : ""}{text}</em> : <em className="dim">词库里没有它的释义</em>
        ) : null}
      </button>
      <span className="pb-trial-sound"><Pronounce word={w} phonetic={gloss?.phonetic ? { uk: gloss.phonetic, us: gloss.phonetic } : undefined} single /></span>
    </div>
  );
}

export function PickBookModal({ open, onClose, onPicked }: {
  open: boolean;
  onClose: () => void;
  onPicked: (bookId: string) => void;
}) {
  const [books, setBooks] = useState<Wordbook[]>(() => getBooks());
  const [group, setGroup] = useState("全部");
  const [kw, setKw] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [trial, setTrial] = useState<Wordbook | null>(null);
  const [trialWords, setTrialWords] = useState<string[]>([]);
  const [trialGloss, setTrialGloss] = useState<Record<string, BookEntry>>({});
  const [source, setSource] = useState<"api" | "mini">("api");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    syncBooks()
      .then((bs) => { if (alive) setBooks(bs); })
      .catch(() => { if (alive) setBooks(getBooks()); });
    return () => { alive = false; };
  }, [open]);

  const list = useMemo(() => {
    const k = kw.trim().toLowerCase();
    return books.filter((b) => (group === "全部" || b.group === group) && (!k || b.name.toLowerCase().includes(k)));
  }, [books, group, kw]);

  const grouped = useMemo(() => {
    const order = ["升学", "大学", "出国"];
    return order
      .map((g) => ({ g, items: list.filter((b) => b.group === g) }))
      .filter((x) => x.items.length > 0)
      .concat([{ g: "其他", items: list.filter((b) => !order.includes(b.group)) }].filter((x) => x.items.length > 0));
  }, [list]);

  const selected = list.find((b) => b.id === selectedId) ?? list[0] ?? null;

  const openTrial = async (b: Wordbook) => {
    setTrial(b);
    setLoading(true);
    setTrialWords(previewWords(b));
    setTrialGloss({});
    try {
      const words = await ensureWords(b, 10);
      setTrialWords(words);
      setTrialGloss(Object.fromEntries(bookGloss(b, 10).map((e) => [e.word.toLowerCase(), e])));
      setSource(bookWordsSource(b));
    } catch {
      setTrialWords(previewWords(b));
      setSource("mini");
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;
  const learned = selected ? learnedCount(selected.id) : 0;

  return (
    <div className="set-mask open" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="stm-modal stm-pick" role="dialog" aria-label="选词书">
        <div className="stm-head">
          <div><h2>{trial ? "试学 · " + trial.name : "挑一本词书"}</h2><p>{trial ? "先看十个词，觉得合适再进去。" : "选一本，进去可以试听十个词再决定。"}</p></div>
          <button type="button" className="stm-x" onClick={onClose} aria-label="关闭">✕</button>
        </div>

        {!trial ? (
          <>
            <div className="stm-toolbar">
              <div className="stm-tabs">{GROUPS.map((g) => <button key={g} type="button" className={group === g ? "on" : ""} onClick={() => setGroup(g)}>{g}</button>)}</div>
              <input className="stm-search" value={kw} onChange={(e) => setKw(e.target.value)} placeholder="找词书" aria-label="找词书" />
            </div>
            <div className="stm-pickbody">
              <div className="stm-list">
                {grouped.map((g) => (
                  <div key={g.g}>
                    <div className="stm-group">{g.g}</div>
                    {g.items.map((b) => <BookRow key={b.id} b={b} selected={selected?.id === b.id} onSelect={() => setSelectedId(b.id)} />)}
                  </div>
                ))}
                {list.length === 0 ? <p className="stm-empty">这一组里没有匹配的词书。</p> : null}
              </div>
              {selected ? (
                <aside className="stm-detail">
                  <div className="stm-cover"><span>{selected.group}</span><b>{selected.name}</b></div>
                  <h3>{selected.name}</h3>
                  <div className="stm-sub">{selected.group} · {selected.total > 0 ? fmtN(selected.total) + " 词" : "词库准备中"} · 难度 {selected.difficulty}/5</div>
                  <div className="stm-progress"><i style={{ width: (selected.total > 0 ? Math.min(100, (learned / selected.total) * 100) : 0) + "%" }} /></div>
                  <div className="stm-nums"><span>已学 {fmtN(learned)}</span><span>{selected.total > 0 ? "还剩 " + fmtN(Math.max(0, selected.total - learned)) : "—"}</span></div>
                  <button type="button" className="stm-primary" onClick={() => void openTrial(selected)}>试学 10 个词</button>
                  <p className="stm-hint">试学只看看深浅，不写进进度。</p>
                </aside>
              ) : null}
            </div>
            <div className="stm-foot"><span>词表来自本地词库，不联网</span><button type="button" className="stm-ghost" onClick={onClose}>取消</button></div>
          </>
        ) : (
          <div className="stm-body">
            <div className="pb-trial-grid">
              <div className="pb-trial-list">
                <div className="pb-trial-hint">先试十个词{loading ? "（加载中…）" : ""}，点词看意思，听发音辨深浅</div>
                {trialWords.map((w, i) => <TrialRow key={w} w={w} i={i} gloss={trialGloss[w.toLowerCase()]} />)}
                {source === "mini" ? <p className="pb-mini-note">当前为内置基础词库，词量有限、仅供参考。</p> : null}
              </div>
              <aside className="pb-archive">
                <div className="pb-archive-head"><span className="pb-cover lg" aria-hidden /><div><b>{trial.name}</b><span className="dim">{trial.group} · 难度 {trial.difficulty}/5</span></div></div>
                <div className="pb-archive-stats"><div><span>全本</span><b>{trial.total > 0 ? fmtN(trial.total) + " 词" : "—"}</b></div><div><span>已学</span><b>{fmtN(learnedCount(trial.id))} 词</b></div><div><span>未学</span><b>{trial.total > 0 ? fmtN(Math.max(0, trial.total - learnedCount(trial.id))) + " 词" : "—"}</b></div></div>
              </aside>
            </div>
            <div className="stm-foot"><button type="button" className="stm-ghost" onClick={() => setTrial(null)}>再看别的</button><button type="button" className="stm-primary" onClick={() => onPicked(trial.id)}>就这本，下一步</button></div>
          </div>
        )}
      </div>
    </div>
  );
}
