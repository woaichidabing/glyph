import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { DictEntry, WordCardState, Wordbook } from "../../types";
import { lookup } from "../../adapters/dictAdapter";
import { bookGloss, bookWords, ensureWords, findBook } from "./wordbooks";
import { buildQuiz } from "./choiceBank";
import {
  GRADES, MASTERY, capAt, gradeOf, stabilityNote,
  type Answer, type Grade,
} from "../../adapters/srs";
import { pronounce } from "../../adapters/pronounce";
import { decDone, forgetCard, freeProgress, masteredToday, setCardState } from "./studyRepo";
import { todayStr } from "../../adapters/streakRepo";
import { useStudy } from "./studyStore";
import { Pronounce } from "../../components/Pronounce";
import { discardScene, fetchScene, type Scene } from "./sceneApi";

type Kind = "new" | "review" | "wrong";
type Mode = "see" | "spell" | "mix";
type EffMode = "see" | "spell";
type Stage = "quiz" | "debrief" | "rest" | "summary";

type Result = { pick: string; ok: boolean; ms: number; streak: number; mastered: boolean; g: Grade };

const KIND_LABEL: Record<Kind, string> = { new: "新学", review: "复习", wrong: "错词" };
const MODE_LABEL: Record<Mode, string> = { see: "看词选义", spell: "听音写词", mix: "听写轮换" };

function Tick({ size = 12 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m2 6.5 3 3 5-7" /></svg>;
}
function Cross({ size = 11 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="m3 3 6 6M9 3l-6 6" /></svg>;
}

/** 冷却抽签：最近 k 个考过的先封起来，从剩下的里随机挑，避免靠短时记忆连过 */
function pickFrom(group: string[], recent: string[], groupSize: number): string | null {
  if (!group.length) return null;
  const k = Math.max(1, Math.min(group.length - 1, groupSize - 2));
  const banned = new Set(recent.slice(-k));
  const free = group.filter((w) => !banned.has(w));
  const from = free.length ? free : group;
  return from[Math.floor(Math.random() * from.length)];
}

function mmss(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}


function wordsOf(kind: Kind, q: ReturnType<typeof useStudy.getState>["queue"]) {
  return kind === "new" ? q.newWords : kind === "review" ? q.reviewWords : q.wrongWords;
}

/**
 * 学习卡：一组词轮着考（冷却抽签），连对三次出师，出师才落 SRS 档位。
 * 两屏：判卷要快（对错一眼看见），讲题要慢（音标释义读完）。
 */
function SceneLine({ text, word }: { text: string; word: string }) {
  const escaped = word.replace(/[^A-Za-z]/g, "");
  const parts = text.split(new RegExp("(" + escaped + ")", "gi"));
  return <>{parts.map((part, i) => part.toLowerCase() === word.toLowerCase() ? <b key={i}>{part}</b> : part)}</>;
}

export function StudyCard({ kind, book, onExit }: { kind: Kind; book: Wordbook; onExit: () => void }) {
  const queue = useStudy((s) => s.queue);
  const cards = useStudy((s) => s.cards);
  const settings = useStudy((s) => s.settings);
  const grade = useStudy((s) => s.grade);
  const markStreak = useStudy((s) => s.markStreak);
  const refresh = useStudy((s) => s.refresh);

  const groupSize = Math.max(2, Math.min(12, settings.groupSize || 5));
  // 自动读音默认关（settings 里没这个字段的老存档也按关算）：先出声容易打扰
  const autoSound = settings.autoSound === true;

  const [mode, setMode] = useState<Mode>("see");
  const [stage, setStage] = useState<Stage>("quiz");
  const [result, setResult] = useState<Result | null>(null);
  const [typed, setTyped] = useState("");
  const [entry, setEntry] = useState<DictEntry | null>(null);
  const [armed, setArmed] = useState(false);
  const [scene, setScene] = useState<Scene | null>(null);
  const [sceneLoading, setSceneLoading] = useState(false);
  const sceneKey = useRef("");

  const undo = useRef<{ word: string; prev: WordCardState | null }[]>([]);
  const shownAt = useRef<number>(Date.now());
  const missed = useRef<Set<string>>(new Set());
  const attempts = useRef<Record<string, number>>({});
  const spellRef = useRef<HTMLInputElement>(null);

  // 开局快照钉死词单（随心背的实时队列会随游标缩短，不能照着取词）
  const [plan] = useState(() => {
    // 新学：把今天已经背会的剔掉再排队。
    // 每日模式的词单当天是冻结的（buildQueue 会原样返回缓存），不剔就会出现
    // "学完退出、再进来还是这一组，而且计数又加一遍"（2026-11-04 真机）。
    const raw = wordsOf(kind, queue);
    const words = kind === "new" ? raw.filter((w) => !masteredToday(w)) : raw;
    const free = kind === "new" && settings.mode === "free";
    const fp = free ? freeProgress() : null;
    return { words, base: fp?.done ?? 0, bookTotal: fp?.total ?? words.length, free };
  });

  const [init] = useState(() => ({ group: plan.words.slice(0, groupSize), pool: plan.words.slice(groupSize) }));
  const [group, setGroup] = useState<string[]>(init.group);
  const [word, setWord] = useState<string | null>(() => pickFrom(init.group, [], groupSize));
  const recent = useRef<string[]>([]);
  const pool = useRef<string[]>(init.pool);
  /** 已入过池的词：随心背续拉时用来挑生词 */
  const seenWords = useRef<Set<string>>(new Set(plan.words));
  const [fetching, setFetching] = useState(false);
  const [noMore, setNoMore] = useState(false);
  const [poolLeft, setPoolLeft] = useState(init.pool.length);
  const [masteredInGroup, setMasteredInGroup] = useState(0);
  /** 今天过掉的词总数（含答错后重考通过、含看答案的） */
  const [passedTotal, setPassedTotal] = useState(0);
  /** 今天没一次过的词（错过的 + 看答案的），收工时统一展示 */
  const [failedToday, setFailedToday] = useState<string[]>([]);
  /** 看过答案的词：小结里不写"错 N 次"，写"看了答案" */
  const [revealedToday, setRevealedToday] = useState<string[]>([]);
  /** 已经 out 出组（step 执行过）的词；撤销时据此决定要不要回退计数 */
  const advanced = useRef<Set<string>>(new Set());
  /** 每个词今天错了几次：小结里给"错 2 次"这种具体信息 */
  const [wrongCounts, setWrongCounts] = useState<Record<string, number>>({});
  /** 小结页要显示的释义：收工时按需查词典 */
  const [sumGlosses, setSumGlosses] = useState<Record<string, string>>({});
  /** 这一组里已出师的词：留在「这一组」里划线展示，不凭空消失（撤销出师要能回来） */
  const [graduated, setGraduated] = useState<string[]>([]);
  const [stats, setStats] = useState({ answers: 0, right: 0, at: Date.now() });

  const total = plan.free ? (book.total || plan.bookTotal) : plan.words.length;
  const freeMode = plan.free;
  const today = todayStr();

  const streakOf = (w: string) => {
    const c = cards[w];
    if (!c) return 0;
    if (c.known) return MASTERY;
    return c.streakAt === today ? Math.min(MASTERY, c.streak ?? 0) : 0;
  };

  const doneN = useMemo(() => {
    const n = plan.words.filter((w) => {
      const c = cards[w];
      if (!c) return false;
      return c.known === true || ((c.streak ?? 0) >= MASTERY && c.streakAt === today);
    }).length;
    return n + (plan.free ? plan.base : 0);
  }, [cards, plan.words, plan.free, plan.base, today]);

  // 干扰项池：已缓存的同书真实词条（每个新词刷新一次，续拉新词后自动纳入）；
  // mini 降级时缓存为空，buildQuiz 自动退回内置 mini 库。
  // memo 依赖用 bookId 而不是 book 对象：findBook 每次返回新对象身份，
  // 答题落库触发重渲染时若跟着重建，quiz 就会重洗牌——选项自己换位置的 bug 就是从这来的。
  const bookId = book?.id ?? "";
  const quizPool = useMemo(
    () => (bookId ? bookGloss(findBook(bookId) ?? book, 10000) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [bookId, word],
  );
  /** 同一道题上屏后冻结：只要词没换，就算 quizPool 身份意外抖动也不重排出选项 */
  const quizRef = useRef<{ w: string | null; quiz: ReturnType<typeof buildQuiz> } | null>(null);
  const quiz = useMemo(() => {
    if (quizRef.current?.w === word) return quizRef.current.quiz;
    const q = word ? buildQuiz(word, quizPool) : null;
    quizRef.current = { w: word, quiz: q };
    return q;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [word, quizPool]);
  const effMode: EffMode =
    mode === "mix" ? ((((word && attempts.current[word]) || 0) % 2 === 0 ? "see" : "spell") as EffMode) : mode;

  useEffect(() => {
    let alive = true;
    setEntry(null);
    if (!word) return;
    lookup(word).then((e) => { if (alive) setEntry(e); }).catch(() => undefined);
    return () => { alive = false; };
  }, [word]);

  useEffect(() => {
    const arm = () => setArmed(true);
    window.addEventListener("pointerdown", arm, { once: true });
    window.addEventListener("keydown", arm, { once: true });
    return () => {
      window.removeEventListener("pointerdown", arm);
      window.removeEventListener("keydown", arm);
    };
  }, []);

  useEffect(() => {
    if (stage === "quiz" && effMode === "spell" && !result) spellRef.current?.focus();
  }, [stage, effMode, result, word]);

  const replay = useCallback(() => { if (word) void pronounce(word, "us"); }, [word]);

  useEffect(() => {
    if (stage !== "summary" || failedToday.length === 0) return;
    let alive = true;
    Promise.all(failedToday.map((w) =>
      lookup(w)
        .then((e) => [w, e ? (e.senses[0]?.translation ?? "") : ""] as const)
        .catch(() => [w, ""] as const),
    )).then((pairs) => {
      if (!alive) return;
      setSumGlosses((m) => {
        const next = { ...m };
        for (const [w, g] of pairs) if (g) next[w] = g;
        return next;
      });
    });
    return () => { alive = false; };
  }, [stage, failedToday.join("|")]);

  const resetTimer = useCallback(() => {
    shownAt.current = Date.now();
    setTyped("");
    setResult(null);
  }, []);

  /**
   * 过一遍就出组（2026-11-03 改）：以前要「组内连对 3 次」才算过，用户反馈等于
   * 在同一组里机械重复。现在每个词当场只考一次，答错立刻重考同一个词；组里每个词
   * 都过一遍 → 这一组结束。抗遗忘交给间隔复习（1/3/7 天）。
   */
  const step = useCallback((w: string, passed: boolean) => {
    let g2 = group;
    let m2 = masteredInGroup;
    if (passed) {
      g2 = group.filter((x) => x !== w);
      m2 += 1;
      setGraduated((gs) => (gs.includes(w) ? gs : [...gs, w]));
      setPassedTotal((n) => n + 1);
      advanced.current.add(w);
    }
    setGroup(g2);
    setMasteredInGroup(m2);
    setWord(pickFrom(g2, recent.current, groupSize));
    resetTimer();
    setStage(g2.length === 0 ? "rest" : "quiz");
  }, [group, masteredInGroup, groupSize, resetTimer]);

  useEffect(() => {
    if (!word) return;
    recent.current = [...recent.current, word].slice(-groupSize);
  }, [word, groupSize]);

  const answer = useCallback((ok: boolean, pick: string) => {
    if (!word || result) return;
    const ms = Math.max(0, Date.now() - shownAt.current);
    if (!ok) missed.current.add(word);
    attempts.current[word] = (attempts.current[word] ?? 0) + 1;
    const streak = ok ? streakOf(word) + 1 : 0;
    const a: Answer = { ok, ms };
    const g = gradeOf(cards[word], a);
    if (ok) {
      // 一次过：直接按 SRS 档位落库（错过的压一级），不再要求连对 3 次
      const final = missed.current.has(word) ? capAt(g, "good") : g;
      undo.current.push({ word, prev: cards[word] ?? null });
      grade(word, final, kind, { ok, ms, streak: MASTERY });

    } else {
      // 这次不算过：记一次失误，同一个词立刻再考一次
      markStreak(word, 0, false, { ok, ms });
      setFailedToday((ws) => (ws.includes(word) ? ws : [...ws, word]));
      setWrongCounts((m) => ({ ...m, [word]: (m[word] ?? 0) + 1 }));
    }
    setStats((s) => ({ ...s, answers: s.answers + 1, right: s.right + (ok ? 1 : 0) }));
    setResult({ pick, ok, ms, streak, mastered: ok, g });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [word, result, cards, grade, markStreak, kind]);

  /** 答错后：同一个词立刻再考一次（题目重排，词不变） */
  const retryWord = useCallback(() => {
    quizRef.current = null;
    resetTimer();
    setStage("quiz");
  }, [resetTimer]);

  /** 先看答案：算今天没过，看完直接进下一个词 */
  const revealAnswer = useCallback(() => {
    if (!word) return;
    const ms = Math.max(0, Date.now() - shownAt.current);
    const g = gradeOf(cards[word], { ok: false, ms });
    undo.current.push({ word, prev: cards[word] ?? null });
    grade(word, g, kind, { ok: false, ms, streak: 0 });
    setFailedToday((ws) => (ws.includes(word) ? ws : [...ws, word]));
    setRevealedToday((ws) => (ws.includes(word) ? ws : [...ws, word]));
    setWrongCounts((m) => ({ ...m, [word]: (m[word] ?? 0) + 1 }));
    setResult({ pick: "", ok: false, ms, streak: 0, mastered: false, g });
    setStage("debrief");
  }, [word, cards, grade, kind]);

  const markKnown = useCallback(() => {
    if (!word) return;
    const ms = Math.max(0, Date.now() - shownAt.current);
    undo.current.push({ word, prev: cards[word] ?? null });
    grade(word, "easy", kind, { ok: true, ms, streak: MASTERY, known: true });
    setStats((s) => ({ ...s, answers: s.answers + 1, right: s.right + 1 }));
    step(word, true);
  }, [word, cards, grade, kind, step]);

  const goNext = useCallback(() => {
    if (stage === "quiz") {
      // 答对了 → 先看一眼这个词（释义/例句）；答错了不往下走，等他用「再试一次」
      if (result?.ok) setStage("debrief");
      return;
    }
    if (stage === "debrief" && word) step(word, true);
  }, [stage, result, step, word]);

  const nextGroup = useCallback(async () => {
    // 随心背深背：本地缓存窗口（首批 600）见底时，按已背进度续拉下一页
    if (freeMode && pool.current.length < groupSize) {
      setFetching(true);
      try {
        const want = plan.base + seenWords.current.size + groupSize * 2;
        await ensureWords(book, want);
        const fresh = bookWords(book, want).filter((w) => !seenWords.current.has(w));
        fresh.forEach((w) => seenWords.current.add(w));
        pool.current.push(...fresh);
      } catch {
        /* 续拉失败就用现有池，mini 降级时本就只有小窗口 */
      } finally {
        setFetching(false);
      }
    }
    const carry = group.slice();
    const extra = pool.current.splice(0, Math.max(0, groupSize - carry.length));
    const g = [...carry, ...extra];
    setPoolLeft(pool.current.length);
    setGroup(g);
    setWord(pickFrom(g, recent.current, groupSize));
    setMasteredInGroup(0);
    setGraduated([]);
    setStats({ answers: 0, right: 0, at: Date.now() });
    resetTimer();
    if (g.length === 0) { setNoMore(true); setStage("rest"); }
    else { setNoMore(false); setStage("quiz"); }
  }, [book, freeMode, group, groupSize, plan.base, resetTimer]);

  const undoLast = useCallback(() => {
    if (stage === "rest" || stage === "summary") return; // 已结算，不能再撤
    const last = undo.current.pop();
    if (!last) return;
    if (last.prev) setCardState(last.prev);
    else forgetCard(last.word);
    decDone(kind);
    // 只有"已经出组"的那一步才回退组进度与过词数；
    // 在答题/详解态撤销时词还没出组，多减一次就会把今天的数算少。
    if (advanced.current.delete(last.word)) {
      setGroup((g) => (g.includes(last.word) ? g : [last.word, ...g]));
      setMasteredInGroup((m) => Math.max(0, m - 1));
      setPassedTotal((n) => Math.max(0, n - 1));
      setGraduated((gs) => gs.filter((x) => x !== last.word));
    }
    setFailedToday((ws) => ws.filter((w) => w !== last.word));
    setRevealedToday((ws) => ws.filter((w) => w !== last.word));
    setWrongCounts((m) => {
      if (!(last.word in m)) return m;
      const next = { ...m };
      delete next[last.word];
      return next;
    });
    setWord(last.word);
    setStage("quiz");
    resetTimer();
    refresh();
  }, [kind, refresh, resetTimer, stage]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key;
      if (k === "ArrowLeft") { e.preventDefault(); undoLast(); return; }
      if (k === "p" || k === "P") { replay(); return; }
      if (k === "k" || k === "K") { if (stage === "quiz" && !result) markKnown(); return; }
      if (k === "Enter" || k === " ") {
        if (document.activeElement === spellRef.current) return;
        if (result || stage === "debrief") { e.preventDefault(); goNext(); }
        return;
      }
      if (stage !== "quiz" || result || !quiz || effMode !== "see") return;
      const n = Number(k);
      if (n >= 1 && n <= quiz.choices.length) {
        const c = quiz.choices[n - 1];
        if (c) answer(c.right, c.word);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [answer, goNext, markKnown, quiz, result, stage, effMode, undoLast, replay]);

  const scenePos = entry?.senses[0]?.pos ?? quiz?.sense?.pos ?? "";
  const sceneZh = entry?.senses[0]?.translation ?? quiz?.sense?.translation ?? "";

  useEffect(() => {
    sceneKey.current = "";
    setScene(null);
    setSceneLoading(false);
  }, [word]);

  useEffect(() => {
    if (stage !== "debrief" || !word) return;
    const key = word + "|" + scenePos;
    if (sceneKey.current === key) return;
    sceneKey.current = key;
    let alive = true;
    const controller = new AbortController();
    setSceneLoading(true);
    void fetchScene(word, scenePos, sceneZh, false, controller.signal)
      .then((next) => { if (alive) setScene(next); })
      .finally(() => { if (alive) setSceneLoading(false); });
    return () => { alive = false; controller.abort(); };
    // 只在详解屏取一次；换词时 key 变
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stage, word, scenePos, sceneZh]);

  const reloadScene = () => {
    if (!word) return;
    setSceneLoading(true);
    void fetchScene(word, scenePos, sceneZh, true)
      .then(setScene)
      .finally(() => setSceneLoading(false));
  };

  if (plan.words.length === 0) {
    return (
      <div className="sc-wrap">
        <div className="dict-state">
          <b>{kind === "review" ? "今天没有到期复习" : kind === "wrong" ? "错词本是空的" : "这本已背完"}</b>
          <span>若想继续，可换一本词书，或先把每日之量调高一些。</span>
          <div className="dict-state-actions"><button type="button" className="btn primary" onClick={onExit}>回背词页</button></div>
        </div>
      </div>
    );
  }

  if (!word && stage === "rest" && group.length === 0 && poolLeft === 0) {
    return (
      <div className="sc-wrap">
        <div className="dict-state">
          <b>{KIND_LABEL[kind]}已毕</b>
          <span>这一轮共 {total} 词，已毕 {doneN} 词。回到背词页可看看另外两条队列。</span>
          <div className="dict-state-actions"><button type="button" className="btn primary" onClick={onExit}>回背词页</button></div>
        </div>
      </div>
    );
  }

  /* ---------------- 结算：背完一组 ---------------- */
  if (stage === "rest") {
    const elapsed = Date.now() - stats.at;
    const allDone = group.length === 0 && (noMore || doneN >= total || (!freeMode && poolLeft === 0));
    return (
      <div className="sc-wrap">
        <div className="sc-sum">
          <p className="sc-sum-kicker">一组已毕</p>
          <h2 className="sc-sum-title">{allDone ? "今天的功课到此为止" : "这一组背完了"}</h2>
          <p className="sc-sum-line">
            {allDone
              ? `共 ${total} 词，今日已毕 ${doneN} 词。回背词页看看另外两条队列。`
              : `这组 ${masteredInGroup} / ${groupSize} 都过了一遍。接着往下就行；今天错过的词，收工时一起看。`}
          </p>
          <div className="sc-rest-stats">
            <div><span>本组过完</span><b>{masteredInGroup} 词</b></div>
            <div><span>用时</span><b>{mmss(elapsed)}</b></div>
            <div><span>一次答对</span><b>{Math.max(0, passedTotal - failedToday.length)} 词</b></div>
          </div>
          <p className="sc-sum-line">{freeMode ? "今天过了 " + doneN + " 个" : "今日已学会 " + doneN + " / " + total}{poolLeft > 0 ? " · 还有 " + poolLeft + " 个没进组" : ""}</p>
          <div className="sc-rest-actions">
            {allDone ? (
              <button type="button" className="btn primary lg" onClick={() => setStage("summary")}>
                看看今天
              </button>
            ) : (
              <>
                <button type="button" className="btn primary lg" onClick={nextGroup} disabled={fetching}>
                  {fetching ? "取词中…" : "继续下一组"}
                </button>
                <button type="button" className="btn ghost lg" onClick={() => setStage("summary")}>
                  今天先到这
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  /* ---------------- 今日小结（收工时看） ---------------- */
  if (stage === "summary") {
    return (
      <div className="sc-wrap">
        <div className="sc-sum">
          <p className="sc-sum-kicker">收工</p>
          <h2 className="sc-sum-title">今天就到这</h2>
          <p className="sc-sum-line">
            过了 {passedTotal} 个词
            {failedToday.length ? ` · 其中 ${failedToday.length} 个没一次过` : " · 全是一次过"}
          </p>
          {failedToday.length ? (
            <div className="sc-sum-rows">
              {failedToday.map((w) => (
                <div className="sc-sum-row" key={w}>
                  <span className="sc-sum-word">{w}</span>
                  <span className="sc-sum-gloss">{sumGlosses[w] || "—"}</span>
                  <span className="sc-sum-meta">
                    {revealedToday.includes(w) ? "看了答案" : `错 ${wrongCounts[w] ?? 1} 次`}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
          <div className="sc-sum-foot">
            <span className="sc-hint">
              {failedToday.length
                ? `明天会先考这 ${failedToday.length} 个，再往后背新的。`
                : "明天按时回来复习就行。"}
            </span>
            <button type="button" className="btn primary lg" onClick={onExit}>好，明天见</button>
          </div>
        </div>
      </div>
    );
  }

  // 结算态（rest / summary）已在上面返回；走到这里还拿不到词，才是异常
  if (!word) return null;

  const phonetic = entry?.phonetic ?? quiz?.phonetic;
  const phonText = phonetic
    ? [phonetic.us, phonetic.uk].filter((x, i, a) => x && a.indexOf(x) === i).join(" · ")
    : "";
  const singleAccent = !phonetic || phonetic.us === phonetic.uk;
  const gloss: DictEntry | null =
    entry ?? (quiz ? { word, phonetic: quiz.phonetic, senses: [quiz.sense], source: "mini" } : null);
  const graded = cards[word];
  const tier = result ? GRADES.find((x) => x.id === result.g) : undefined;
  const masked = effMode === "spell" && stage === "quiz" && !result;

  return (
    <div className="sc-wrap">
      <header className="sc-head">
        <div className="sc-head-l">
          <b>{freeMode ? "随心背" : KIND_LABEL[kind]}</b>
          <span>{freeMode ? "已过 " + doneN + " 个 · 本组 " + masteredInGroup + " / " + groupSize : "已学会 " + doneN + " / " + total + " · 本组 " + masteredInGroup + " / " + groupSize}</span>
        </div>
        <div className="sc-head-r">
          <div className="seg sc-seg">
            {(["see", "spell", "mix"] as Mode[]).map((m) => (
              <button key={m} type="button" className={mode === m ? "on" : ""}
                onClick={() => { setMode(m); resetTimer(); }}>{MODE_LABEL[m]}</button>
            ))}
          </div>
          <button type="button" className="sc-exit" onClick={onExit}>退出</button>
        </div>
      </header>

      <div className="sc-progress"><i style={{ width: `${Math.round((doneN / total) * 100)}%` }} /></div>

      <div className="sc-groupchips">
        <span className="sc-group-label">这一组{graduated.length > 0 ? ` · 已出组 ${graduated.length}` : ""}</span>
        {graduated.map((w) => (
          <span key={w} className="sc-gchip done">
            <span className="sc-gchip-w">{w}</span>
          </span>
        ))}
        {group.map((w) => {
          const cur = w === word;
          return (
            <span key={w} className={"sc-gchip" + (cur ? " on" : "")}>
              {/* 只遮当前词：别的词的明文不泄露答案，全遮连出师战绩都看不见 */}
              {masked && cur ? (
                <span className="sc-gchip-mask" role="img" aria-label="词被遮住了" />
              ) : (
                <span className="sc-gchip-w">{w}</span>
              )}
              <></>
            </span>
          );
        })}
      </div>

      {stage === "quiz" ? (
        <div className="card sc-card">
          <div className="sc-prompt">
            {/* 听写轮换：同一个词先看词、再听写，徽章把"这词考哪种"亮出来 */}
            {mode === "mix" && !result ? (
              <span className="sc-mixbadge" title="听写轮换：同一个词先看词，再听写">
                {effMode === "spell" ? "这个词考听写" : "这个词考看词"}
              </span>
            ) : null}
            {masked ? (
              <>
                <button type="button" className="btn lg sc-play-main" onClick={replay}>▶ 播一遍</button>
                <span className="sc-hint">没听清就再点一次 · P 键同效</span>
              </>
            ) : (
              <>
                <div className="sc-prompt-row">
                  <span className="sc-word">{word}</span>
                </div>
                {phonetic ? <span className="sc-phn">{phonText}</span> : null}
                <div className="sc-say"><Pronounce word={word} phonetic={phonetic} autoPlay={armed && autoSound} single={singleAccent} hidePhonetic /></div>
              </>
            )}
          </div>

          {effMode === "see" && quiz ? (
            <>
              <div className="sc-qlabel">下面哪一条是它的意思</div>
              <div className="sc-choices">
                {quiz.choices.map((c, i) => {
                  const st = !result ? "idle" : c.right ? "right" : c.word === result.pick ? "wrong" : "dim";
                  return (
                    <button key={c.word} type="button" className={"sc-choice " + st}
                      onClick={() => answer(c.right, c.word)} disabled={!!result} data-state={st}>
                      <span className="sc-choice-key">{i + 1}</span>
                      <span className="sc-choice-pos">{c.pos}</span>
                      <span className="sc-choice-text">{c.text}</span>
                      {st === "right" ? <span className="sc-choice-mark ok"><Tick /></span> : null}
                      {st === "wrong" ? <span className="sc-choice-mark no"><Cross /></span> : null}
                    </button>
                  );
                })}
              </div>
            </>
          ) : null}

          {effMode === "see" && !quiz ? (
            <div className="sc-two">
              <button type="button" className="btn ghost lg" onClick={() => answer(false, word)} disabled={!!result}>没见过</button>
              <button type="button" className="btn primary lg" onClick={() => answer(true, word)} disabled={!!result}>认得</button>
            </div>
          ) : null}

          {effMode === "spell" ? (
            <div>
              <div className="sc-qlabel">拼出来 <em>{word.length} 个字母</em></div>
              <input ref={spellRef} value={typed} className={"sc-spell" + (result ? (result.ok ? " right" : " wrong") : "")}
                onChange={(e) => setTyped(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && typed.trim() && !result) {
                    e.preventDefault();
                    answer(typed.trim().toLowerCase() === word.toLowerCase(), typed.trim());
                  }
                }}
                disabled={!!result} placeholder="拼出来，回车交卷" aria-label="拼写你听到的词"
                autoComplete="off" autoCapitalize="off" spellCheck={false} />
              {!result ? (
                <div className="sc-spell-row">
                  <button type="button" className="sc-link" onClick={() => answer(false, typed.trim() || "（没拼出来）")}>想不起来，直接看答案</button>
                  <button type="button" className="btn primary sm"
                    disabled={!typed.trim()}
                    onClick={() => answer(typed.trim().toLowerCase() === word.toLowerCase(), typed.trim() || "（没拼出来）")}>交卷</button>
                </div>
              ) : (
                <div className="sc-verdict">
                  {result.ok
                    ? <span className="ok">拼对了 · {result.pick}</span>
                    : <>你写的是 <b className="no">{result.pick}</b></>}
                </div>
              )}
            </div>
          ) : null}

          <div className="sc-bottom">
            {!result ? (
              <>
                <button type="button" className="sc-known" onClick={markKnown}><Tick size={11} /> 熟词，这个早就会了</button>
                <span className="sc-hint">
                  {effMode === "spell" ? "回车交卷 · P 再听一遍 · K 熟词" : "1–4 选答案 · P 发音 · K 熟词"}
                </span>
              </>
            ) : result.ok ? (
              <>
                <button type="button" className="btn primary lg" onClick={goNext}>看一遍再继续</button>
                <button type="button" className="sc-link" onClick={undoLast}>撤销上一次评分</button>
              </>
            ) : (
              <>
                <button type="button" className="btn primary lg" onClick={retryWord}>再试一次</button>
                <button type="button" className="sc-link" onClick={revealAnswer}>先看答案（算今天没过）</button>
              </>
            )}
          </div>

          {result ? (
            <div className="sc-result">
              <div className="sc-result-line">
                <span className={result.ok ? "ok" : "no"}>
                  {result.ok ? "对了" : effMode === "spell" ? "拼错了" : "选错了"}
                </span>
                <span className="sc-result-note">
                  {result.ok ? "明天会再考一次" : "这个词今天还没过，再答对一次就往下走"}
                </span>
              </div>
            </div>
          ) : null}
        </div>
      ) : (
        /* ---------------- 第二屏：详解 ---------------- */
        <div className="card sc-card">
          <div className="sc-qlabel">{result?.mastered ? "这个词已出组" : "再看一眼"}</div>

          <div className="sc-debrief-head">
            <div className="sc-prompt-row">
              <span className="sc-word">{word}</span>
            </div>
            {graded && (graded.seen ?? 0) > 1 ? (
              <span className="sc-hint">见过 {graded.seen} 次 · 对 {graded.right} 次</span>
            ) : null}
          </div>

          {phonetic ? <div className="sc-phn">{phonText}</div> : null}
          <div style={{ marginTop: 8 }}><Pronounce word={word} phonetic={phonetic} autoPlay={armed && autoSound} single={singleAccent} hidePhonetic /></div>

          <div className="sc-gloss">
            {gloss?.senses.length ? (
              gloss.senses.map((s, i) => (
                <div key={i} className="sense-block">
                  <div className="sense">{s.pos ? <i className="pos">{s.pos}</i> : null}<span>{s.translation}</span></div>
                  {s.examples.slice(0, 1).map((ex, j) => (
                    <div key={j} className="e-ex">
                      <p className="ex-en">{ex.en}</p>
                      <p className="ex-cn">{ex.zh}</p>
                    </div>
                  ))}
                </div>
              ))
            ) : (
              <span className="sc-hint">这个词库里没有它的释义</span>
            )}
            {gloss?.forms?.length ? <div className="sc-forms">变形 {gloss.forms.join(" / ")}</div> : null}
          </div>


          {sceneLoading && !scene ? <p className="sc-hint" style={{ marginTop: 10 }}>正在找一句场景…</p> : null}
          {scene ? (
            <div className="sc-scene">
              <div className="sc-scene-head"><span>场景</span><span>{scene.source === "tatoeba" ? "例句库" : "临时例句"}</span></div>
              <p className="sc-scene-en"><SceneLine text={scene.en} word={word} /></p>
              <p className="sc-scene-cn">{scene.zh}</p>
              {scene.source === "generated" ? (
                <div className="sc-scene-actions">
                  <button type="button" className="sc-link" onClick={reloadScene} disabled={sceneLoading}>{sceneLoading ? "换一句…" : "换一句"}</button>
                  <button type="button" className="sc-link" onClick={() => { void discardScene(word); setScene(null); }}>这句不合适</button>
                </div>
              ) : null}
            </div>
          ) : null}

          {result ? (
            <div className={"sc-srs " + (result.ok ? "ok" : "no")}>
              <div>
                {result.ok ? (
                  <>这个词今天<b>过了</b> · <b>{graded?.intervalDays ?? tier?.days ?? 1}</b> 天后再考
                    {missed.current.has(word) ? <span className="sc-hint"> · 中途错了一次，下次早点考</span> : null}</>
                ) : (
                  <>这次不算过 · 再答对一次就进下一个词，明天仍会再考</>
                )}
              </div>
              {graded?.s ? (
                <div className="sc-hint" style={{ marginTop: 6 }}>
                  来的时候还记得 {Math.round((graded.lastR ?? 0.9) * 100)}% · {stabilityNote(graded.s)} · 难度 {(graded.d ?? 5).toFixed(1)}
                </div>
              ) : null}
            </div>
          ) : null}

          {/* 这一屏是"再看一眼"：看完就进下一个词（看答案进来的也算今天没过，同样往下走） */}
          <div className="sc-bottom">
            <button type="button" className="btn primary lg" onClick={goNext}>下一个词</button>
            <button type="button" className="sc-link" onClick={undoLast}>撤销上一次评分</button>
          </div>
        </div>
      )}

      <div className="sc-foot">
        <button type="button" className="sc-link" onClick={undoLast}>← 撤销上一次评分</button>
        <span className="sc-hint">{stage === "quiz" ? (effMode === "spell" ? "回车交卷 · P 再听一遍" : "1–4 选答案 · P 发音") : "回车继续"}</span>
      </div>
    </div>
  );
}
