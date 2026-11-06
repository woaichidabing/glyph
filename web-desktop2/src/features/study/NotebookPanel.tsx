import { useEffect, useMemo, useState } from "react";
import type { DictEntry, WordbookItem } from "../../types";
import { lookup } from "../../adapters/dictAdapter";
import { exportBookAsText } from "../wordbook";
import { useWordbook } from "../wordbook";
import { Pronounce } from "../../components/Pronounce";

/**
 * 生词本面板（从背词页头部进入）。
 * 两条旧规矩保留：默认只给英文+音标，中文**双击**单词掀开；双击「一眼看全」掀全部。
 * 单击不掀开，避免划过页面误放答案。
 */
export function NotebookPanel({ onBack, onGoDict }: { onBack: () => void; onGoDict: () => void }) {
  const items = useWordbook((s) => s.items);
  const bookReady = useWordbook((s) => s.ready);
  const remove = useWordbook((s) => s.remove);
  const restore = useWordbook((s) => s.restore);
  const clear = useWordbook((s) => s.clear);

  // 直接进生词本（没经过词典页）也要把后端快照拉下来；store 内部并发去重
  useEffect(() => {
    if (!bookReady) void useWordbook.getState().hydrate();
  }, [bookReady]);

  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [allOn, setAllOn] = useState(false);
  const [entries, setEntries] = useState<Record<string, DictEntry>>({});
  const [q, setQ] = useState("");
  const [lastRemoved, setLastRemoved] = useState<WordbookItem | null>(null);

  const list = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return kw ? items.filter((i) => i.word.toLowerCase().includes(kw)) : items;
  }, [items, q]);

  const shown = (word: string) => allOn || !!revealed[word];
  const want = list.filter((i) => shown(i.word) && !entries[i.word.toLowerCase()]).map((i) => i.word);

  useEffect(() => {
    if (want.length === 0) return;
    let alive = true;
    Promise.all(want.map((w) => lookup(w).catch(() => null))).then((got) => {
      if (!alive) return;
      setEntries((m) => {
        const next = { ...m };
        got.forEach((e) => { if (e) next[e.word.toLowerCase()] = e; });
        return next;
      });
    });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [want.join("|"), allOn]);

  const glossOf = (item: WordbookItem) => {
    const e = entries[item.word.toLowerCase()];
    return e?.senses[0]?.translation ?? item.gloss ?? "—";
  };

  /** 移出后显示“撤销”：只有真删掉了才给撤销（失败回滚时不给假的撤销） */
  const dropItem = async (item: WordbookItem) => {
    const removed = await remove(item.word);
    if (removed) setLastRemoved(removed);
  };

  const download = () => {
    const text = exportBookAsText(items);
    if (!text) return;
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "生词本.txt";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="nb-wrap">
      <header className="nb-head">
        <div>
          <button type="button" className="sc-link" onClick={onBack}>← 回背词页</button>
          <h2 className="nb-title">收进来的词</h2>
          <p className="nb-sub">默认只给英文与音标。想看中文，<b>双击</b>那个词；一次看完，双击右边「一眼看全」。</p>
        </div>
        <div className="nb-tools">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="找词"
            aria-label="在生词本里找词" className="set-input nb-search" />
          <button type="button" className={"pb-chip" + (allOn ? " on" : "")}
            onDoubleClick={() => setAllOn((v) => !v)}
            title="双击显示/隐藏全部释义" aria-label="双击：显示或隐藏全部释义">
            {allOn ? "全部已显" : "一眼看全"}
          </button>
        </div>
      </header>

      {list.length === 0 ? (
        <div className="dict-state">
          <b>{items.length === 0 ? "生词本还是空的" : "没有匹配的词"}</b>
          <span>{items.length === 0 ? "查词时点「收进生词本」，它就落到这里。" : "换个关键词再找找。"}</span>
          {items.length === 0 ? (
            <div className="dict-state-actions"><button type="button" className="btn primary" onClick={onGoDict}>去查一个词</button></div>
          ) : null}
        </div>
      ) : (
        <>
          <div className="nb-list">
            {list.map((it) => {
              const open = shown(it.word);
              return (
                <div key={it.word} className="nb-row">
                  <div className="nb-main" title="双击看释义"
                    onDoubleClick={() => setRevealed((m) => ({ ...m, [it.word]: !m[it.word] }))}>
                    <div className="nb-wordline">
                      <span className="nb-word">{it.word}</span>
                      <span className="nb-phn">{it.phonetic?.uk || it.phonetic?.us || "—"}</span>
                    </div>
                    {open ? <div className="nb-gloss">{glossOf(it)}</div> : null}
                  </div>
                  <div className="nb-row-actions">
                    <Pronounce word={it.word} phonetic={it.phonetic} hidePhonetic single />
                    <button type="button" className="sc-link"
                      onClick={() => { void dropItem(it); }}>
                      移出
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          <div className="nb-foot">
            <span className="sc-hint">共 {items.length} 词</span>
            <button type="button" className="btn ghost sm" onClick={download}>导出为文本</button>
            <button type="button" className="sc-link" onClick={() => void clear()} disabled={items.length === 0}>清空生词本</button>
            <button type="button" className="sc-link" style={{ marginLeft: "auto" }} onClick={onGoDict}>去查一词 →</button>
          </div>

          {lastRemoved ? (
            <div className="nb-undo">
              <span>已移出 {lastRemoved.word}</span>
              <button type="button" className="btn primary sm"
                onClick={() => {
                  const item = lastRemoved;
                  setLastRemoved(null);
                  void restore(item);
                }}>撤销</button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
