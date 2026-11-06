import { useEffect, useMemo, useRef, useState } from "react";
import { useStudy } from "./studyStore";
import { homeAllDone, pickPrimary } from "./studyHome";
import { getSettings, learnedCount, freeProgress } from "./studyRepo";
import { readStreak, checkIn, type Streak } from "../../adapters/streakRepo";
import { getBooks, syncBooks, ensureWords, findBook, bookWordsSource } from "./wordbooks";
import { PickBookModal } from "./PickBookModal";
import { SetupModal } from "./SetupModal";
import { StudyCard } from "./StudyCard";
import { NotebookPanel } from "./NotebookPanel";
import { ErrorBookPanel } from "./ErrorBookPanel";
import { useStore } from "../../store";
import { useWordbook } from "../wordbook";
import type { Wordbook } from "../../types";

type Kind = "new" | "review" | "wrong";

const META: Record<Kind, { label: string; note: string }> = {
  new: { label: "新学", note: "按每日之量从词书里取" },
  review: { label: "复习", note: "到期的词，由复习算法推送" },
  wrong: { label: "错词", note: "反复错的词，不占新学额度" },
};

function QueueRow({ kind, total, done, free, selected, onSelect }: {
  kind: Kind;
  total: number;
  done: number;
  free: { left: number; done: number; total: number; cursor: number; word: string | null } | null;
  selected: boolean;
  onSelect: () => void;
}) {
  const m = META[kind];
  const empty = total === 0 && !free;
  const pct = free
    ? (free.total > 0 ? Math.round((free.done / free.total) * 100) : 0)
    : (total > 0 ? Math.round((done / total) * 100) : 0);
  const label = free ? "随心背" : m.label;
  const note = empty
    ? (kind === "review" ? "今天没有到期复习" : kind === "wrong" ? "错词本是空的" : "这本已背完")
    : free
      ? "不设额度，从上次停下的地方接着走"
      : m.note;
  const state = empty
    ? "—"
    : free
      ? (free.done > 0 ? "已背会 " + free.done + " 个" : "未开始")
      : done + " / " + total;

  return (
    <button
      type="button"
      className={"sv-row" + (empty ? " empty" : "") + (selected ? " selected" : "")}
      aria-pressed={selected}
      disabled={empty}
      onClick={onSelect}
    >
      <div className="sv-row-copy">
        <div className="sv-row-t">{label}</div>
        <div className="sv-row-s">{note}</div>
      </div>
      <div className="sv-bar"><i style={{ width: pct + "%" }} /></div>
      <span className="sv-row-state">{state}</span>
    </button>
  );
}


const UNIT_SIZE = 20;

function fmtN(n: number): string {
  return n.toLocaleString("en-US");
}

function BookPage({ book, learned, streak, free, onBack, onStart, onPick }: {
  book: Wordbook;
  learned: number;
  streak: Streak;
  free: boolean;
  onBack: () => void;
  onStart: () => void;
  onPick: () => void;
}) {
  const unitCount = book.total > 0 ? Math.ceil(book.total / UNIT_SIZE) : 0;
  const currentUnit = unitCount > 0 ? Math.min(unitCount, Math.max(1, Math.floor(learned / UNIT_SIZE) + 1)) : 0;
  const start = unitCount > 0 ? Math.max(1, Math.min(currentUnit - 2, Math.max(1, unitCount - 5))) : 0;
  const end = unitCount > 0 ? Math.min(unitCount, start + 5) : 0;
  const units = Array.from({ length: Math.max(0, end - start + 1) }, (_, i) => start + i);
  const pct = book.total > 0 ? Math.min(100, Math.round((learned / book.total) * 100)) : 0;
  const next = Math.min(book.total || learned + 1, learned + 1);
  const finished = book.total > 0 && learned >= book.total;

  return (
    <div className="sv-bookpage">
      <header className="sv-bookhead">
        <div>
          <div className="sv-eyebrow">背词 / 书本</div>
          <h1>{book.name}</h1>
          <p>{book.group || "词书"} · 连续 {streak.current} 天</p>
        </div>
        <div className="sv-bookhead-actions">
          <button type="button" className="sv-textbtn" onClick={onBack}>回今天</button>
          <button type="button" className="sv-textbtn" onClick={onPick}>换书</button>
        </div>
      </header>

      <div className="sv-bookhero">
        <div className="sv-bookcover">
          <span className="sv-bookcover-top">{book.group || "WORDS"}</span>
          <span className="sv-bookcover-title">{book.name}</span>
        </div>
        <div className="sv-bookinfo">
          <div className="sv-eyebrow">当前词书</div>
          <h2>{learned === 0 ? "还没开始背" : <>已学 <span className="sv-count">{fmtN(learned)} / {book.total ? fmtN(book.total) : "—"}</span></>}</h2>
          <p>{free ? "随心背，不设额度；从上次停下的地方接着走。" : "按词书顺序背；每 20 个词一个单元。"}</p>
          <div className="sv-bookprogress"><i style={{ width: pct + "%" }} /></div>
          <div className="sv-bookmeta"><span>共 {book.total ? fmtN(book.total) : "—"} 词</span><span>{unitCount ? fmtN(unitCount) + " 个单元" : "单元未同步"}</span></div>
          <div className="sv-bookactions">
            <button type="button" className="sv-primary" onClick={onStart} disabled={book.total === 0}>{finished ? "从头再背" : "继续第 " + next + " 个"}</button>
          </div>
        </div>
      </div>

      {unitCount > 0 ? (
        <div className="sv-units">
          <div className="sv-unithead"><span>#</span><span>单元</span><span>进度</span><span>状态</span></div>
          {units.map((n) => {
            const unitDone = n < currentUnit ? UNIT_SIZE : n === currentUnit ? Math.max(0, Math.min(UNIT_SIZE, learned - (n - 1) * UNIT_SIZE)) : 0;
            const unitTotal = n === unitCount && book.total > 0 ? book.total - (n - 1) * UNIT_SIZE : UNIT_SIZE;
            const state = n < currentUnit ? "已完成" : n === currentUnit ? (unitDone > 0 ? unitDone + " / " + unitTotal : "还没开始") : "尚未开始";
            return (
              <div key={n} className={"sv-unitrow" + (n === currentUnit ? " current" : "") + (n < currentUnit ? " done" : "")}>
                <span className="sv-unitn">{String(n).padStart(2, "0")}</span>
                <div><div className="sv-unitt">第 <span className="sv-inline-num">{n}</span> 单元</div><div className="sv-unitsub">前 {fmtN(unitTotal)} 个词</div></div>
                <div className="sv-unitbar"><i style={{ width: (unitDone / unitTotal) * 100 + "%" }} /></div>
                <span className="sv-unitstate">{state}</span>
              </div>
            );
          })}
          {start > 1 || end < unitCount ? <p className="sv-hint">共 {fmtN(unitCount)} 个单元，这里只显示当前附近；换书或继续背都可以。</p> : null}
        </div>
      ) : (
        <p className="sv-hint">词表还没同步，先回今天开始背；词数到位后再显示单元。</p>
      )}

      <footer className="sv-foot"><span>{book.name} / 书本</span><span>本地 · 自动保存</span></footer>
    </div>
  );
}

export default function StudyView({ hidden }: { hidden: boolean }) {
  const settings = useStudy((s) => s.settings);
  const queue = useStudy((s) => s.queue);
  const refresh = useStudy((s) => s.refresh);
  const rebuild = useStudy((s) => s.rebuild);

  const dev = import.meta.env.DEV ? new URLSearchParams(location.search) : null;
  const [pickOpen, setPickOpen] = useState(() => !settings.bookId);
  const [bookOpen, setBookOpen] = useState(false);
  const [selectedKind, setSelectedKind] = useState<Kind>("new");
  const [setupOpen, setSetupOpen] = useState(false);
  const [studyKind, setStudyKind] = useState<Kind | null>(() => {
    const k = dev?.get("kind");
    return k === "new" || k === "review" || k === "wrong" ? k : null;
  });
  const [ready, setReady] = useState(false);
  const [panel, setPanel] = useState<null | "words" | "errors">(() => {
    const p = dev?.get("panel");
    return p === "words" || p === "errors" ? p : null;
  });
  const [source, setSource] = useState<"api" | "mini">("api");
  const [streak, setStreak] = useState<Streak>(() => readStreak());
  const celebrated = useRef(false);
  const planNotified = useRef(false);

  const book = settings.bookId ? findBook(settings.bookId) : undefined;

  useEffect(() => {
    if (hidden) return;
    let alive = true;
    (async () => {
      if (dev?.get("seedbook")) {
        const { addError } = await import("./studyRepo");
        const wb = useWordbook;
        [
          { word: "commit", phon: "kəˈmɪt", zh: "承诺，犯（罪），投入" },
          { word: "ambiguous", phon: "æmˈbɪɡjuəs", zh: "模棱两可的，含糊的" },
          { word: "persuade", phon: "pəˈsweɪd", zh: "说服，劝服" },
        ].forEach((x) => wb.getState().add({
          word: x.word, source: "mini",
          phonetic: { uk: x.phon, us: x.phon },
          senses: [{ pos: "v.", translation: x.zh, examples: [] }],
        }));
        addError({ word: "complement", from: "作文 My Dream Job",
          sentence: "The two approaches complement each other.",
          reason: "与 compliment（恭维）混淆", lapses: 2 });
        addError({ word: "receive", from: "作文 A Letter", reason: "拼写漏了 i before e", lapses: 1 });
        useStudy.getState().refresh();
      }
      let books;
      try {
        books = await syncBooks();
      } catch {
        books = getBooks();
      }
      let s = getSettings();
      const devBook = dev?.get("book");
      if (!s.bookId && devBook && books.some((x) => x.id === devBook)) {
        useStudy.getState().patchSettings({ bookId: devBook });
        s = getSettings();
      }
      const b = books.find((x) => x.id === s.bookId);
      let gotReal = false;
      if (b && b.total > 0) {
        try {
          await ensureWords(b, 600);
          if (alive) { setSource("api"); gotReal = true; }
        } catch {
          if (alive) setSource(bookWordsSource(b));
        }
      } else if (b) {
        if (alive) setSource(bookWordsSource(b));
      }
      if (alive) {
        // 词书加载完只补空队列；已有今日队列（尤其背完的）不能强制重排，
        // 否则 doneNew 归零、背过的词会被当成新词重新发一遍。
        const q = useStudy.getState().queue;
        const emptyQueue = q.newWords.length === 0 && q.reviewWords.length === 0 && q.wrongWords.length === 0
          && q.doneNew === 0 && q.doneReview === 0 && q.doneWrong === 0;
        if (gotReal && emptyQueue) rebuild(); else refresh();
        setStreak(readStreak());
        setReady(true);
      }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hidden]);

  const blocks = useMemo(() => [
    { kind: "new" as Kind, total: queue.newWords.length, done: queue.doneNew },
    { kind: "review" as Kind, total: queue.reviewWords.length, done: queue.doneReview },
    { kind: "wrong" as Kind, total: queue.wrongWords.length, done: queue.doneWrong },
  ], [queue]);

  const free = settings.mode === "free" ? freeProgress() : null;
  // 错词也是一天的功课；随心背没有“今日已毕”这回事。
  const allDone = homeAllDone(blocks, !!free);
  const newDone = blocks[0].total === 0 || free ? null : blocks[0].done >= blocks[0].total;
  const reviewDone = blocks[1].total === 0 ? null : blocks[1].done >= blocks[1].total;
  const remaining = blocks.reduce((n, b) => {
    if (b.kind === "new" && free) return n + free.left;
    return n + Math.max(0, b.total - b.done);
  }, 0);
  const doneSegments = blocks.filter((b) => b.total > 0 && b.done >= b.total).length;
  const totalSegments = blocks.filter((b) => b.total > 0).length;
  // 主按钮永远指向还没做完的那一段；完成的段不再冒充下一段。
  const selectedBlock = pickPrimary(blocks, !!free, selectedKind);
  const primary = selectedBlock?.kind ?? null;
  const segmentText = totalSegments === 0
    ? "未排"
    : doneSegments === 0
      ? "还没开始"
      : doneSegments === totalSegments
        ? "全部完成"
        : doneSegments + " / " + totalSegments + " 段";
  const primaryDone = selectedBlock?.done ?? 0;
  const primaryLabel = allDone
    ? "今日已毕"
    : primary === "new" && free
      ? "继续随心背"
      : primary
        ? (primaryDone > 0 ? "继续" : "开始") + (primary === "new" ? "新学" : primary === "review" ? "复习" : "错词") + " · 第 " + (primaryDone + 1) + " 个"
        : "今日已毕";

  useEffect(() => {
    if (hidden || studyKind) return;
    if (!allDone) { celebrated.current = false; planNotified.current = false; return; }
    if (celebrated.current) return;
    celebrated.current = true;
    setStreak(checkIn());
    if (!planNotified.current) {
      planNotified.current = true;
      void useStore.getState().notifyPlanEvent("study", "今日背词完成");
    }
  }, [allDone, hidden, studyKind]);

  const onPicked = (bookId: string) => {
    useStudy.getState().patchSettings({ bookId });
    // 换书必须重排；只改设置（节奏 / 每组）不重排。
    useStudy.getState().rebuild();
    setPickOpen(false);
    setSetupOpen(true);
  };

  const exitStudy = () => {
    setStudyKind(null);
    refresh();
    setStreak(readStreak());
  };

  if (hidden) {
    return <section className="view" id="view-study" hidden />;
  }

  if (studyKind && book && ready) {
    return <StudyCard kind={studyKind} book={book} onExit={exitStudy} />;
  }

  if (panel === "words") {
    return <NotebookPanel onBack={() => setPanel(null)} onGoDict={() => useStore.getState().setView("dict")} />;
  }
  if (panel === "errors") {
    return (
      <ErrorBookPanel
        onBack={() => setPanel(null)}
        onStudyWrong={() => { setPanel(null); setStudyKind("wrong"); }}
      />
    );
  }

  const fmt = (n: number) => n.toLocaleString("en-US");
  const date = new Date();
  const dateText = (date.getMonth() + 1) + " 月 " + date.getDate() + " 日 · 周" + ["日", "一", "二", "三", "四", "五", "六"][date.getDay()];
  const learned = book ? learnedCount(book.id) : 0;

  return (
    <section className="view sv-view" id="view-study" hidden={hidden}>
      <div className="sv-wrap">
        {bookOpen && book ? (
          <BookPage
            book={book}
            learned={learned}
            streak={streak}
            free={!!free}
            onBack={() => setBookOpen(false)}
            onStart={() => { setBookOpen(false); setStudyKind("new"); }}
            onPick={() => setPickOpen(true)}
          />
        ) : !book ? (
          <div className="sv-first">
            <div className="sv-eyebrow">背词 / 第一次</div>
            <h1>先选一本词书。</h1>
            <p>选一本你现在最需要的；之后随时能换，已学的词不会丢。</p>
            <button type="button" className="sv-primary" onClick={() => setPickOpen(true)}>去选词书</button>
          </div>
        ) : (
          <>
            <header className="sv-head">
              <div className="sv-head-left">
                <div className="sv-eyebrow">背词 / 今天</div>
                <h1>今天背这一组</h1>
                <p><span>{dateText}</span><span>{book.name}</span><span>连续 {streak.current} 天</span></p>
              </div>
              <div className="sv-head-right">
                <div className="sv-seal">连续<span>{streak.current} 天</span></div>
                <div className="sv-actions">
                  <button type="button" onClick={() => setPanel("words")}>生词本</button>
                  <button type="button" onClick={() => setPanel("errors")}>错词本</button>
                  <button type="button" onClick={() => setSetupOpen(true)}>改怎么背</button>
                  <button type="button" onClick={() => setBookOpen(true)}>看书本</button>
                  <button type="button" onClick={() => setPickOpen(true)}>换词书</button>
                </div>
              </div>
            </header>

            <div className="sv-bookline">
              <span className="sv-label">词书</span>
              <b>{book.name}</b>
              <span>已学 {fmt(learned)} / {book.total ? fmt(book.total) : "—"} 词</span>
              <span className="sv-bookline-sp" />
              <span>{free ? "随心背" : "每日 " + settings.dailyNew + " 词"}</span>
            </div>

            <div className="sv-grid">
              <div className="sv-main">
                <div className="sv-hero">
                  <div><h2>{free ? (free.cursor === 0 ? <>从第 <span className="sv-inline-num">1</span> 个开始</> : <>接着第 <span className="sv-inline-num">{free.cursor + 1}</span> 个往下背</>) : remaining > 0 ? <>今天还剩 <span className="sv-inline-num">{remaining}</span> 个词</> : "今天的词已经清空"}</h2><p>{free ? "随心背，不设额度；从上次停下的地方接着走。" : "新学、复习、错词各算各的；做完一段就可以停。"}</p></div>
                  <span className="sv-hero-num">{segmentText}</span>
                </div>

                {allDone ? <div className="sv-notice done">今天的功课已毕 · 已记一课，连续 {streak.current} 日不辍</div> : null}
                {newDone === true && reviewDone === false ? (
                  <div className="sv-notice"><span>新学已毕，复习尚余 <b>{blocks[1].total - blocks[1].done}</b> 项。</span><button type="button" onClick={() => setStudyKind("review")}>续完复习</button></div>
                ) : null}
                {reviewDone === true && newDone === false ? (
                  <div className="sv-notice"><span>复习已毕，新学尚余 <b>{blocks[0].total - blocks[0].done}</b> 项。</span><button type="button" onClick={() => setStudyKind("new")}>续完新学</button></div>
                ) : null}

                <div className="sv-queue">
                  {blocks.map((b) => (
                    <QueueRow key={b.kind} kind={b.kind} total={b.total} done={b.done}
                      free={b.kind === "new" ? free : null}
                      selected={b.kind === primary}
                      onSelect={() => setSelectedKind(b.kind)} />
                  ))}
                </div>

                <div className="sv-start">
                  {primary && !allDone ? <button type="button" className="sv-primary" onClick={() => setStudyKind(primary)}>{primaryLabel}</button> : <span className="sv-allclear">今天已清空。</span>}
                  <span>{primary && !allDone ? "做完这一段就休息，不用一次清完。" : "明天见。"}</span>
                </div>

                <p className="sv-hint">没背完也没关系。剩下的词会留到明天，不催你。</p>
                {source === "mini" ? <p className="sv-hint">当前用的是内置基础词库；完整词库稍后自动接上。</p> : null}
              </div>

              <aside className="sv-aside">
                <h3>这一本</h3>
                <div className="sv-stats">
                  <div><span>已学</span><b>{fmt(learned)}</b></div>
                  <div><span>词数</span><b>{book.total ? fmt(book.total) : "—"}</b></div>
                  <div><span>每日</span><b>{free ? "随心" : settings.dailyNew}</b></div>
                  <div><span>连续</span><b>{streak.current}</b></div>
                </div>
                <p className="sv-aside-note"><b>{book.name}</b><br />{free ? "从上次停下的地方接着走。" : "每天 " + settings.dailyNew + " 个新词，复习按到期推。"}</p>
                <button type="button" className="sv-textbtn" onClick={() => setPickOpen(true)}>换一本词书</button>
              </aside>
            </div>

            <footer className="sv-foot"><span>背词 / {book.name}</span><span>本地 · 自动保存</span></footer>
          </>
        )}
      </div>

      <PickBookModal open={pickOpen} onClose={() => setPickOpen(false)} onPicked={onPicked} />
      {book ? <SetupModal open={setupOpen} book={book} onClose={() => setSetupOpen(false)} /> : null}
    </section>
  );
}
