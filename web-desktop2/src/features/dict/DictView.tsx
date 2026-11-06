import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, FormEvent } from "react";
import type { DictEntry, HistoryItem, WordbookItem } from "../../types";
import { useStore } from "../../store";
import { lookup, NotFoundError, hotWords } from "../../adapters/dictAdapter";
import { pronounce } from "../../adapters/pronounce";
import { useWordbook } from "../wordbook";

type Status = "idle" | "loading" | "ready" | "empty" | "error";

/** "今天 / 昨天 / 10 月 18 日"：记录卡里只写人话，不写时间戳 */
function formatWhen(ts?: number): string {
  if (!ts) return "—";
  const d = new Date(ts);
  const now = new Date();
  const same = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, now)) return "今天";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (same(d, y)) return "昨天";
  return d.getFullYear() === now.getFullYear()
    ? d.getMonth() + 1 + " 月 " + d.getDate() + " 日"
    : d.getFullYear() + " 年 " + (d.getMonth() + 1) + " 月 " + d.getDate() + " 日";
}

/** ECDICT 的音标里混着西里尔字母 ә 和 ASCII 重音符，显示前统一成 IPA 常用形 */
function ipaOf(entry: DictEntry): string {
  const raw = (entry.phonetic?.uk || entry.phonetic?.us || "").trim();
  if (!raw) return "";
  const fixed = raw.replace(/ә/g, "ə").replace(/'/g, "ˈ");
  return fixed.startsWith("/") ? fixed : "/" + fixed + "/";
}

function posOf(entry: DictEntry): string {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const sense of entry.senses) {
    const p = (sense.pos || "").trim();
    if (p && !seen.has(p)) {
      seen.add(p);
      out.push(p);
    }
  }
  return out.slice(0, 3).join(" / ");
}

function firstGloss(entry: DictEntry): string {
  return (entry.senses[0]?.translation || "").trim();
}

function SpeakButton({ word }: { word: string }) {
  const [playing, setPlaying] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
  }, []);
  const speak = async () => {
    setPlaying(true);
    await pronounce(word, "us");
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setPlaying(false), 900);
  };
  return (
    <button
      type="button"
      className={"dp-spk" + (playing ? " on" : "")}
      onClick={() => void speak()}
      title="播放发音"
      aria-label="播放发音"
    >
      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <path d="M4 8v4h3l4 3V5L7 8H4z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
        <path d="M13.6 7.6c.9.7 1.4 1.5 1.4 2.4s-.5 1.7-1.4 2.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
      </svg>
    </button>
  );
}

type EntryViewProps = {
  entry: DictEntry;
  historyItem?: HistoryItem;
  savedItem?: WordbookItem;
  recent: HistoryItem[];
  noteOpen: boolean;
  loading: boolean;
  collecting: boolean;
  error: string | null;
  onToggleNote: () => void;
  onCollect: () => void;
  onAsk: () => void;
  onGo: (word: string) => void;
};

function EntryView({
  entry, historyItem, savedItem, recent, noteOpen, loading, collecting, error,
  onToggleNote, onCollect, onAsk, onGo,
}: EntryViewProps) {
  const seen = historyItem?.count ?? (historyItem ? 1 : 0);
  const saved = !!savedItem;
  const ipa = ipaOf(entry);
  const pos = posOf(entry);
  const forms = entry.forms ?? [];
  const tags = entry.tags ?? [];
  const defs = entry.definitions ?? [];

  return (
    <div className={"dp-entry" + (loading ? " dp-loading" : "")} key={entry.word}>
      <div className="dp-gutter"><span className="dp-rule" /></div>

      <div className="dp-wordcol">
        <h1 className="dp-hw">{entry.word}</h1>
        <div className="dp-pron">
          <SpeakButton word={entry.word} />
          {ipa ? <span className="dp-ipa">{ipa}</span> : null}
          {pos ? <span className="dp-pos">{pos}</span> : null}
        </div>
        {entry.queried_form && entry.base_form ? (
          <p className="dp-formnote">
            你查的是 <b>{entry.queried_form}</b>，这是它的原形 <b>{entry.base_form}</b>。
          </p>
        ) : null}

        <ol className="dp-senses">
          {entry.senses.map((sense, i) => (
            <li key={sense.pos + "-" + i} style={{ "--i": i } as CSSProperties}>
              <span className="dp-no">{i + 1}</span>
              <div>
                {sense.pos ? <span className="dp-posline">{sense.pos}</span> : null}
                <p className="dp-def">{sense.translation || "—"}</p>
                {sense.examples?.slice(0, 1).map((ex, j) => (
                  <p className="dp-ex" key={j}>
                    {ex.en}
                    {ex.zh ? <span className="dp-ex-zh">{ex.zh}</span> : null}
                  </p>
                ))}
              </div>
            </li>
          ))}
        </ol>

        {defs.length ? (
          <div className="dp-defs">
            <div className="dp-meta-h">英文释义</div>
            {defs.map((line, i) => <p key={i}>{line}</p>)}
          </div>
        ) : null}
      </div>

      <aside className="dp-margin">
        {forms.length || tags.length ? (
          <div className="dp-meta">
            {forms.length ? (
              <div className="dp-meta-block">
                <div className="dp-meta-h">词形</div>
                <div className="dp-forms">
                  {forms.map((f, i) => <span key={i}>{f}</span>)}
                </div>
              </div>
            ) : null}
            {tags.length ? (
              <div className="dp-meta-block">
                <div className="dp-meta-h">考试</div>
                <div className="dp-tags">{tags.join("、")}</div>
              </div>
            ) : null}
          </div>
        ) : null}

        <div className={"dp-mnote" + (noteOpen ? " open" : "")}>
          <div className="dp-mnote-h">
            <span className="dp-m">M</span>
            <span>{seen > 0 ? "查过 " + seen + " 次" : "第一次查"}</span>
            {noteOpen ? (
              <button type="button" className="dp-mclose" onClick={onToggleNote}>收起</button>
            ) : null}
          </div>

          <button
            type="button"
            className="dp-textbtn dp-ask-open"
            onClick={onToggleNote}
            aria-expanded={noteOpen}
          >
            问 Glyph
          </button>

          <div className="dp-askbody" aria-hidden={!noteOpen}>
            <div className="dp-record">
              <span>{"最近 " + (seen > 0 ? formatWhen(historyItem?.at) : "—")}</span>
              <span className="dp-rec-dot">·</span>
              <span>{saved ? "收进生词本 " + formatWhen(savedItem?.addedAt) : "尚未收进生词本"}</span>
            </div>
            <p className="dp-ask-p">
              {"词库里有 " + entry.senses.length + " 条释义，第一条是「" + (firstGloss(entry) || "—") + "」。"}
            </p>
            <p className="dp-ask-p dp-ask-muted">更细的用法，去对话里问。</p>
            <button
              type="button"
              className="dp-textbtn"
              onClick={onAsk}
              tabIndex={noteOpen ? 0 : -1}
            >
              在对话里继续问
            </button>
          </div>

          <div className="dp-mnote-actions">
            <button
              type="button"
              className={"dp-collect" + (saved ? " done" : "")}
              aria-pressed={saved}
              disabled={collecting || saved}
              onClick={onCollect}
            >
              <svg className="dp-check" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M3.5 8.5l3 3 6-6.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className="label">{saved ? "已收进生词本" : collecting ? "正在收…" : "收进生词本"}</span>
            </button>
            {saved ? (
              <button type="button" className="dp-undo" disabled={collecting} onClick={onCollect}>
                撤销
              </button>
            ) : null}
          </div>
          {error ? <p className="dp-warn">{error}</p> : null}
        </div>

        {recent.length ? (
          <div className="dp-recent">
            <div className="dp-meta-h">最近</div>
            {recent.map((h) => (
              <button key={h.word} type="button" onClick={() => onGo(h.word)}>{h.word}</button>
            ))}
          </div>
        ) : null}
      </aside>
    </div>
  );
}

export default function DictView({ hidden }: { hidden: boolean }) {
  const setView = useStore((s) => s.setView);
  const setAskDraft = useStore((s) => s.setAskDraft);
  const toggleSb = useStore((s) => s.toggleSb);
  const sbHidden = useStore((s) => s.sbHidden);

  const [q, setQ] = useState(() => {
    if (!import.meta.env.DEV) return "";
    const params = new URLSearchParams(location.search);
    return params.get("q") ?? params.get("word") ?? "";
  });
  const [entry, setEntry] = useState<DictEntry | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [flash, setFlash] = useState("");
  const [noteOpen, setNoteOpen] = useState(false);
  const [collecting, setCollecting] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const flashTimer = useRef<number | null>(null);
  // dev 走查用：?view=dict&q=commit&ask=1 直接展开页边批注（和样稿的 ?ask=1 同义）
  const askOnLoad = useRef(import.meta.env.DEV && new URLSearchParams(location.search).get("ask") === "1");

  const items = useWordbook((s) => s.items);
  const history = useWordbook((s) => s.history);
  const ready = useWordbook((s) => s.ready);
  const storeError = useWordbook((s) => s.error);
  const add = useWordbook((s) => s.add);
  const remove = useWordbook((s) => s.remove);
  const remember = useWordbook((s) => s.remember);

  const flashNow = useCallback((msg: string) => {
    setFlash(msg);
    if (flashTimer.current) window.clearTimeout(flashTimer.current);
    flashTimer.current = window.setTimeout(() => setFlash(""), 2400);
  }, []);

  const runNow = useCallback(async (raw: string) => {
    const w = raw.trim();
    abort.current?.abort();
    if (!w) {
      setEntry(null);
      setStatus("idle");
      setFlash("");
      return;
    }
    const ac = new AbortController();
    abort.current = ac;
    setStatus("loading");
    setFlash("");
    try {
      const found = await lookup(w, ac.signal);
      if (ac.signal.aborted) return;
      setEntry(found);
      setStatus("ready");
      setNoteOpen(askOnLoad.current);
      askOnLoad.current = false;
      void useStore.getState().notifyPlanEvent("dict", "查过 " + found.word);
      void remember(found.word).catch(() => {});
    } catch (err) {
      if (ac.signal.aborted) return;
      if (err instanceof NotFoundError) {
        // 查不到就换成空状态，别把上一个词的释义留在屏幕上冒充结果
        setEntry(null);
        setStatus("empty");
        setFlash("");
      } else {
        setStatus(entry ? "ready" : "error");
        flashNow("词库暂时连不上");
      }
    }
  }, [entry, flashNow, remember]);

  useEffect(() => {
    const initial = q.trim();
    if (initial) void runNow(initial);
    // 只在进页面时查一次；之后的查询走回车 / 最近
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => {
    abort.current?.abort();
    if (flashTimer.current) window.clearTimeout(flashTimer.current);
  }, []);

  const key = entry?.word.toLowerCase() ?? "";
  const historyItem = useMemo(
    () => history.find((h) => h.word.toLowerCase() === key),
    [history, key],
  );
  const savedItem = useMemo(
    () => items.find((i) => i.word.toLowerCase() === key),
    [items, key],
  );
  const recent = useMemo(
    () => history.filter((h) => h.word.toLowerCase() !== key).slice(0, 2),
    [history, key],
  );
  const idleRecent = history.slice(0, 4);

  const submit = (ev: FormEvent) => {
    ev.preventDefault();
    const w = q.trim();
    if (!w) return;
    void runNow(w);
  };

  const go = (word: string) => {
    setQ(word);
    setNoteOpen(false);
    void runNow(word);
  };

  const collect = async () => {
    if (!entry || collecting) return;
    setCollecting(true);
    try {
      if (savedItem) await remove(entry.word);
      else await add(entry);
    } catch {
      /* 错误已由 store 记在 error 里，界面下面那行会显示 */
    } finally {
      setCollecting(false);
    }
  };

  const askAbout = () => {
    if (!entry) return;
    const seen = historyItem?.count ?? (historyItem ? 1 : 0);
    const bits = ["我在词典里查了 " + entry.word];
    bits.push(
      seen > 0
        ? "之前查过 " + seen + " 次，最近一次是" + formatWhen(historyItem?.at)
        : "这是第一次查",
    );
    bits.push(
      savedItem
        ? "已经收进生词本（" + formatWhen(savedItem.addedAt) + "）"
        : "还没收进生词本",
    );
    setAskDraft(
      bits.join("，")
      + "。这些只是词典里的查词和收录记录，不代表我在对话里用过这个词。"
      + "请讲讲这个词的用法、常见搭配和容易用错的地方。",
    );
    setView("ask");
  };

  const askAboutWord = (word: string) => {
    const w = word.trim();
    if (!w) return;
    setAskDraft("我在词典里查不到 " + w + "。请讲讲这个词的意思、用法和常见搭配。");
    setView("ask");
  };

  return (
    <section className="view dict-page" id="view-dict" hidden={hidden}>
      <div className="dp-main">
        <header className="dp-top">
          <button
            type="button"
            className="dp-sbtoggle"
            title={sbHidden ? "展开侧栏" : "收起侧栏"}
            aria-expanded={!sbHidden}
            onClick={toggleSb}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M9 4v16" />
            </svg>
          </button>
          <div className="dp-page-name">词典</div>
          <span className="dp-top-sp" />
          <form className="dp-search" role="search" onSubmit={submit} autoComplete="off">
            <svg width="15" height="15" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <circle cx="8.8" cy="8.8" r="5.6" stroke="currentColor" strokeWidth="1.4" />
              <path d="M13.1 13.1L17 17" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
            <input
              value={q}
              onChange={(e) => { setQ(e.target.value); setFlash(""); }}
              placeholder="查一个词"
              aria-label="查一个词"
              spellCheck={false}
              autoFocus={!hidden}
            />
            <kbd>Enter</kbd>
            {flash ? <span className="dp-notfound" role="status">{flash}</span> : null}
          </form>
        </header>

        <div className="dp-scroll">
          {!entry && status === "idle" ? (
            <div className="dp-idle">
              
              <h2>{idleRecent.length ? "接着上次查。" : "今天想查哪个词？"}</h2>
              <p>
                {idleRecent.length
                  ? "最近查过的词在下面。也可以直接输入新的词。"
                  : "输入英文单词，回车。查过的词会留在下面，下次从这里接着看。"}
              </p>
              <div className="dp-idle-label">{idleRecent.length ? "最近查过" : "从这些开始"}</div>
              <div className="dp-links">
                {idleRecent.length
                  ? idleRecent.map((h) => (
                      <button key={h.word} type="button" onClick={() => go(h.word)}>
                        {h.word}<span className="dp-idle-meta">{formatWhen(h.at)}</span>
                      </button>
                    ))
                  : hotWords().slice(0, 5).map((w) => (
                      <button key={w} type="button" onClick={() => go(w)}>{w}</button>
                    ))}
              </div>
            </div>
          ) : null}

          {!entry && status === "loading" ? (
            <div className="dp-sk" aria-busy="true">
              <div className="dp-sk-line dp-sk-h" />
              <div className="dp-sk-line dp-sk-w70" />
              <div className="dp-sk-line dp-sk-w55" />
              <div className="dp-sk-line dp-sk-w80" />
              <div className="dp-sk-line dp-sk-w45" />
            </div>
          ) : null}

          {!entry && status === "empty" ? (
            <div className="dp-state">
              <h2>「{q.trim()}」不在本地词库里</h2>
              <p>换个拼写，或者查动词原形。也可以直接去对话里问。</p>
              <div className="dp-state-actions">
                <button type="button" className="dp-textbtn" onClick={() => askAboutWord(q)}>去对话里问</button>
              </div>
            </div>
          ) : null}

          {!entry && status === "error" ? (
            <div className="dp-state">
              <h2>词库暂时连不上</h2>
              <p>本地词库没给出结果。稍后再试，或先去对话里问。</p>
              <div className="dp-state-actions">
                <button type="button" className="dp-textbtn" onClick={() => void runNow(q)}>重试</button>
                <button type="button" className="dp-textbtn" onClick={() => askAboutWord(q)}>去对话里问</button>
              </div>
            </div>
          ) : null}

          {entry ? (
            <EntryView
              entry={entry}
              historyItem={historyItem}
              savedItem={savedItem}
              recent={recent}
              noteOpen={noteOpen}
              loading={status === "loading"}
              collecting={collecting}
              error={storeError ? "记录暂时没保存上，稍后再试。" : null}
              onToggleNote={() => setNoteOpen((v) => !v)}
              onCollect={() => void collect()}
              onAsk={askAbout}
              onGo={go}
            />
          ) : null}

          <div className="dp-foot">
            <span>来源 本地词库</span>
            <span>{ready ? (storeError ? "记录未同步" : "记录已同步") : "正在读记录…"}</span>
          </div>
        </div>
      </div>
    </section>
  );
}
