import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type PointerEvent } from "react";
import {
  aidAllowed,
  aidLabelFor,
  formatClock,
  gapBlanks,
  isAnswered,
  makeAnchor,
  missingNumbers,
  moveGapOption,
  optionUsedBy,
  refById,
  remainingMs,
  renderSegments,
  stateOf,
  VIEW_TITLE,
  toggleDoubt,
  toggleInState,
  withAnswer,
  type PassageBlock,
  type PassageDetail,
  type QuestionState,
  type ReadingAttempt,
  type ReadingNote,
  type RenderMark,
} from "./reading";
import type { AttemptPatch } from "./readingAttempts";
import { audioUrl, lookup } from "../../adapters/dictAdapter";

type Tool = "cursor" | "pen" | "eraser";
type Selection = { blockId: string; text: string; start: number; end: number; quote: string; x: number; y: number };

const PEN = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9" /><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" /></svg>
);
const ERASER = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m7 21-4-4L14 6a2 2 0 0 1 3 3l-8 8" /><path d="M14 4l6 6" /><path d="M5 21h14" /></svg>
);
const CURSOR = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m4 4 7.5 17 2-7.5L21 11.5z" /></svg>
);
const CLEAR = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="M19 6l-1 14H6L5 6" /></svg>
);
const CLOCK = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="13" r="8" /><path d="M12 9v4l2 2" /><path d="M5 3 2 6" /><path d="m22 6-3-3" /></svg>
);

/** 选区 → 原句锚点。跨块的选区一律不记（宁可没有，不要错位）。 */
function readSelection(): Selection | null {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  const startNode = range.startContainer;
  const endNode = range.endContainer;
  const holderOf = (n: Node) => {
    const el = n.nodeType === 1 ? (n as Element) : n.parentElement;
    return el?.closest?.("[data-block]") as HTMLElement | null;
  };
  const holder = holderOf(startNode);
  if (!holder || holder !== holderOf(endNode)) return null;

  const walker = document.createTreeWalker(holder, NodeFilter.SHOW_TEXT);
  let seen = 0;
  let start = -1;
  let end = -1;
  while (walker.nextNode()) {
    const t = walker.currentNode as Text;
    const len = t.nodeValue?.length ?? 0;
    if (t === startNode) start = seen + range.startOffset;
    if (t === endNode) end = seen + range.endOffset;
    seen += len;
  }
  const text = holder.textContent ?? "";
  if (start < 0 || end < 0 || end <= start) return null;
  const rect = range.getBoundingClientRect();
  return {
    blockId: holder.dataset.block ?? "",
    text,
    start,
    end,
    quote: text.slice(start, end),
    x: rect.left,
    y: rect.top,
  };
}

function occurrencesBefore(text: string, quote: string, end: number): number {
  if (!quote) return 0;
  let count = 0;
  let from = 0;
  while (true) {
    const idx = text.indexOf(quote, from);
    if (idx < 0 || idx >= end) break;
    count += 1;
    from = idx + 1;
  }
  return Math.max(0, count - 1);
}

export default function Practice({
  detail,
  attempt,
  saving,
  submitting,
  saveError,
  onBack,
  onPatch,
  onSubmit,
}: {
  detail: PassageDetail;
  attempt: ReadingAttempt;
  saving: boolean;
  submitting: boolean;
  saveError: string;
  onBack: () => void;
  onPatch: (next: ReadingAttempt, patch: AttemptPatch) => Promise<boolean>;
  onSubmit: () => void;
}) {
  const structure = detail.structure;
  const view = structure.view;
  const refs = structure.question_refs;

  const [notesOpen, setNotesOpen] = useState(false);
  const [tool, setTool] = useState<Tool>("cursor");
  const [penColor, setPenColor] = useState("#c2402a");
  const [penWidth, setPenWidth] = useState(2.5);
  const [sel, setSel] = useState<Selection | null>(null);
  const [noteDraft, setNoteDraft] = useState(false);
  const [noteText, setNoteText] = useState("");
  const [activeId, setActiveId] = useState<string>(refs[0]?.question_id ?? "");
  const [toast, setToast] = useState("");
  const [pendingMove, setPendingMove] = useState<{ question_id: string; letter: string; from: string } | null>(null);
  const [confirmSubmit, setConfirmSubmit] = useState<number[] | null>(null);
  const [timerOpen, setTimerOpen] = useState(false);
  const [now, setNow] = useState<number>(() => Date.now());

  const exam = attempt.mode === "exam";
  const aidsOk = aidAllowed(attempt);

  // 倒计时：存的是绝对 deadline，这里只是把「现在」往前推，刷新不重置
  useEffect(() => {
    if (!attempt.deadline) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [attempt.deadline]);

  const left = remainingMs(attempt.deadline, now);

  useEffect(() => {
    if (left !== null && left <= 0 && !submitting) onSubmit();
  }, [left, submitting, onSubmit]);

  const showToast = (m: string) => {
    setToast(m);
    window.setTimeout(() => setToast(""), 2200);
  };

  const commitStates = useCallback(
    (states: QuestionState[], changed: string[]) => {
      const next = { ...attempt, questions: states };
      void onPatch(next, {
        questions: states.filter(q => changed.includes(q.question_id)),
      });
    },
    [attempt, onPatch],
  );

  const commitNotes = useCallback(
    (notes: ReadingNote[], changedId?: string) => {
      const next = { ...attempt, notes };
      const patch: AttemptPatch = changedId
        ? { notes: notes.filter(n => n.id === changedId) }
        : { notes: [] };
      void onPatch(next, patch);
    },
    [attempt, onPatch],
  );

  const pickAnswer = (question_id: string, letter: string) => {
    if (view === "gap") {
      const holder = optionUsedBy(attempt.questions, letter);
      if (holder && holder.question_id !== question_id) {
        const fromNo = refById(structure, holder.question_id)?.number ?? 0;
        setPendingMove({ question_id, letter, from: String(fromNo) });
        return;
      }
    }
    const states = withAnswer(attempt.questions, question_id, letter);
    commitStates(states, [question_id]);
  };

  const doClear = (question_id: string) => {
    const states = attempt.questions.filter(q => q.question_id !== question_id);
    commitStates(states, [question_id]);
    const patch: AttemptPatch = { remove_questions: [question_id] };
    void onPatch({ ...attempt, questions: states }, patch);
  };

  const doDoubt = (question_id: string) => {
    const states = toggleDoubt(attempt.questions, question_id);
    commitStates(states, [question_id]);
  };

  const doToggle = (question_id: string, field: "candidates" | "excluded", value: string) => {
    const states = toggleInState(attempt.questions, question_id, field, value);
    commitStates(states, [question_id]);
  };

  const addNote = (kind: ReadingNote["kind"], body: string, question_id: string | null, selNow: Selection | null) => {
    if (!body.trim()) return;
    const anchor = selNow
      ? makeAnchor({
        blockId: selNow.blockId,
        blockText: selNow.text,
        quote: selNow.quote,
        occurrence: occurrencesBefore(selNow.text, selNow.quote, selNow.end),
        revision: attempt.passage_revision,
      })
      : null;
    const note: ReadingNote = {
      id: `n-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      kind,
      question_id,
      anchor,
      body: body.trim(),
      created_at: new Date().toISOString(),
    };
    commitNotes([...attempt.notes, note], note.id);
  };

  const removeNote = (id: string) => {
    const notes = attempt.notes.filter(n => n.id !== id);
    commitNotes(notes);
    void onPatch({ ...attempt, notes }, { remove_notes: [id] });
  };

  const captureSelection = (_e: MouseEvent) => {
    if (tool !== "cursor") return;
    const info = readSelection();
    setSel(info);
  };

  useEffect(() => {
    const off = () => setSel(null);
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, []);

  const doLookup = (text: string) => {
    void lookup(text)
      .then(entry => {
        addNote("word", `${text}　${entry.senses[0]?.translation ?? ""}`.trim(), null, sel);
        showToast(`${text}　${entry.senses[0]?.translation ?? "已标为生词"}`);
      })
      .catch(() => {
        addNote("word", text, null, sel);
        showToast(`${text}　词典没查到，已标为生词（不是错，是暂时查不到）`);
      });
    setSel(null);
  };

  const doLine = () => {
    if (sel) addNote("line", sel.quote, activeId || null, sel);
    setSel(null);
  };

  const confirmNote = () => {
    if (noteText.trim()) addNote("note", noteText, activeId || null, sel);
    setNoteDraft(false);
    setNoteText("");
    setSel(null);
  };

  // 题目某块的笔记标记（"生词 / 自划句"按锚点画，不是靠猜文本位于第几段）
  const marksFor = (block: PassageBlock): RenderMark[] =>
    (attempt.notes ?? [])
      .filter(n => n.anchor && n.anchor.block_id === block.id)
      .map(n => ({
        start: n.anchor!.start,
        end: n.anchor!.end,
        kind: n.kind === "word" ? "word" : "line",
      }));

  const answered = (attempt.questions ?? []).filter(isAnswered).length;

  const requestSubmit = () => {
    const missing = missingNumbers(structure, attempt);
    if (missing.length) {
      setConfirmSubmit(missing);
      return;
    }
    onSubmit();
  };

  const startExam = (minutes: number) => {
    void onPatch(
      { ...attempt, mode: "exam", deadline: new Date(Date.now() + minutes * 60_000).toISOString() },
      { mode: "exam", deadline: new Date(Date.now() + minutes * 60_000).toISOString() },
    );
    setTimerOpen(false);
    setNow(Date.now());
    showToast(`限时 ${minutes} 分钟开始，到点自动交卷`);
  };

  const dropTimer = () => {
    void onPatch({ ...attempt, deadline: null }, { deadline: null });
    setNow(Date.now());
  };

  const letters: string[] = view === "matching" ? structure.picks : Object.keys(structure.options).sort();

  return (
    <div className="rs-workspace rs-practice">
      <header className="rs-head">
        <button className="rs-back" onClick={onBack}>← 题库</button>
        <div>
          <div className="rs-htitle">{VIEW_TITLE[view] ?? detail.qtype}</div>
          <div className="rs-hsub">
            {[detail.exam, detail.year ? `${detail.year}年` : "", detail.source].filter(Boolean).join(" · ")}
          </div>
        </div>
        <div className="rs-hsp" />
        <span className="rs-save" role="status">
          {submitting ? "正在判卷…" : saving ? "正在保存…" : saveError ? "刚才这一步没存上" : "已自动保存"}
        </span>
        <div className="rs-prog">已答 <b>{answered}</b> / {refs.length}</div>
        <div className="rs-timerarea">
          {left != null ? (
            <div className={"timer-run" + (left <= 60_000 ? " urgent" : "")}>
              <span className="tr-t">{formatClock(left)}</span>
              <button className="tr-x" title="取消限时" onClick={dropTimer}>×</button>
            </div>
          ) : (
            <button className="timer-set" onClick={() => setTimerOpen(true)}>{CLOCK}{exam ? "限时中（未设定）" : "设定时间"}</button>
          )}
          {timerOpen ? (
            <div className="timer-pop open">
              <div className="tp-h">设定倒计时</div>
              <div className="tp-n">单篇时长是你自己的训练设置，不是官方统一时限；不设定时则不限时。</div>
              <div className="tp-presets">
                {[10, 15, 20, 25, 30].map(m => (
                  <button key={m} className="tp-preset" onClick={() => startExam(m)}>{m} 分钟</button>
                ))}
              </div>
              <button className="tp-off" onClick={() => setTimerOpen(false)}>取消</button>
            </div>
          ) : null}
        </div>
        <div className="seg2">
          <button className={exam ? "" : "on"} onClick={() => void onPatch({ ...attempt, mode: "practice" }, { mode: "practice" })}>自由练习</button>
          <button className={exam ? "on ex" : "ex"} onClick={() => { setTimerOpen(true); void onPatch({ ...attempt, mode: "exam" }, { mode: "exam" }); }}>限时模拟</button>
        </div>
        <button className="btn primary sm" onClick={requestSubmit} disabled={submitting}>
          {submitting ? "判卷中…" : "我做完了"}
        </button>
      </header>

      {saveError ? <div className="rs-banner warn" role="alert">{saveError}</div> : null}
      {attempt.stage === "pending_submission" ? (
        <div className="rs-banner warn" role="status">
          <span>上次交卷没判成（作答已保存），可以再点一次「我做完了」。</span>
        </div>
      ) : null}
      {exam && attempt.aids_used.length ? (
        <div className="rs-banner note" role="status">
          这次作答在自由练习阶段用过辅助：{attempt.aids_used.map(a => aidLabelFor(a)).join("、")}（记录会保留，不冒充独立完成）。
        </div>
      ) : null}

      <div className="rs-grid">
        <div className={"rs-col rs-passage" + (tool !== "cursor" ? " draw" : "")} data-draw={tool}>
          <div className="rs-phead">
            <span className="tag">{HEAD_TAG[view] ?? "PASSAGE"}</span>
            <span className="ttl">
              {view === "matching"
                ? `共 ${structure.picks.length} 段 · ${refs.length} 题`
                : view === "gap"
                  ? `共 ${gapBlanks(structure).length} 空 · ${Object.keys(structure.options).length} 个候选句`
                  : `${refs.length} 题`}
            </span>
          </div>
          <div className="rs-passage-inner" onMouseUp={captureSelection}>
            <AnnotLayer key={detail.quiz_id} quizId={detail.quiz_id} tool={tool} color={penColor} width={penWidth} />
            {structure.blocks.map(b => {
              if (b.kind === "blank") {
                const st = stateOf(attempt, b.question_id ?? "");
                const no = refById(structure, b.question_id ?? "")?.number ?? b.blank_no ?? 0;
                return (
                  <span
                    key={b.id}
                    className={"gap-slot" + (isAnswered(st) ? " filled" : "") + (activeId === b.question_id ? " active" : "")}
                    role="button"
                    tabIndex={0}
                    onClick={() => setActiveId(b.question_id ?? "")}
                    onKeyDown={e => { if (e.key === "Enter" || e.key === " ") setActiveId(b.question_id ?? ""); }}
                  >
                    {isAnswered(st) ? `${no}. ${structure.options[st.answer] ?? st.answer}` : `${no}. （点这里选句子）`}
                  </span>
                );
              }
              return (
                <p className={"rs-p" + (view === "matching" ? " matched" : "")} key={b.id} data-block={b.id}>
                  {b.label ? <span className="pn">{b.label}</span> : null}
                  {renderSegments(b.text, marksFor(b)).map((s, j) =>
                    s.kind === "word" ? <span className="w-word" key={j}>{s.t}</span>
                      : s.kind === "line" ? <span className="w-line" key={j}>{s.t}</span>
                        : <span key={j}>{s.t}</span>,
                  )}
                  {view === "matching" ? (
                    <span className="pm-pmarks">
                      {stateOf(attempt, activeId).candidates.includes(b.id) ? <span className="pm-tag q">暂定</span> : null}
                      {stateOf(attempt, activeId).excluded.includes(b.id) ? <span className="pm-tag x">已排除</span> : null}
                      <span className="pm-ptools">
                        <button onClick={() => doToggle(activeId, "candidates", b.id)}>暂定</button>
                        <button className={stateOf(attempt, activeId).excluded.includes(b.id) ? "off" : ""} onClick={() => doToggle(activeId, "excluded", b.id)}>
                          {stateOf(attempt, activeId).excluded.includes(b.id) ? "撤销排除" : "排除"}
                        </button>
                        <button onClick={() => pickAnswer(activeId, b.id)}>选这段</button>
                      </span>
                    </span>
                  ) : null}
                </p>
              );
            })}
          </div>
        </div>

        <div className="rs-col rs-questions">
          {view === "gap" ? (
            <div className="gap-options">
              <div className="gap-oh">
                {activeId
                  ? `给第 ${refById(structure, activeId)?.number ?? ""} 题选一句`
                  : "先点原文里的空位"}
              </div>
              {letters.map(L => {
                const usedBy = optionUsedBy(attempt.questions, L);
                const mine = stateOf(attempt, activeId).answer === L;
                return (
                  <button
                    key={L}
                    className={"rs-opt gap-opt" + (mine ? " on" : "") + (usedBy && !mine ? " used" : "")}
                    onClick={() => activeId && pickAnswer(activeId, L)}
                  >
                    <span className="ol">{L}</span>{structure.options[L]}
                    {usedBy && !mine
                      ? <span className="usedby">已用在第 {refById(structure, usedBy.question_id)?.number ?? "?"} 题</span>
                      : null}
                  </button>
                );
              })}
            </div>
          ) : null}

          {refs.map(ref => {
            const question = detail.questions.find(q => q.number === ref.number);
            const st = stateOf(attempt, ref.question_id);
            const myNotes = attempt.notes.filter(n => n.question_id === ref.question_id);
            return (
              <div
                className={"rs-q" + (st.doubt ? " doubt" : "") + (activeId === ref.question_id ? " active" : "")}
                key={ref.question_id}
                onFocus={() => setActiveId(ref.question_id)}
                onClick={() => setActiveId(ref.question_id)}
              >
                <div className="rs-qtop">
                  <span className="rs-qno">{ref.number}</span>
                  <div className="rs-qstem">{question?.stem ?? ""}</div>
                  {view === "matching" ? (
                    <span className="pm-pickset">
                      {structure.picks.map(L => (
                        <button
                          key={L}
                          className={"pm-letter" + (st.answer === L ? " on" : "") + (st.excluded.includes(L) ? " ex" : "")}
                          onClick={() => pickAnswer(ref.question_id, L)}
                          title={st.excluded.includes(L) ? "这题你排除过这段" : ""}
                        >{L}</button>
                      ))}
                      <button className="pm-clear" onClick={() => doClear(ref.question_id)}>清空</button>
                    </span>
                  ) : view === "gap" ? (
                    <button className={"pm-pick" + (st.answer ? " set" : "")} onClick={() => setActiveId(ref.question_id)}>
                      {st.answer || "—"}
                    </button>
                  ) : null}
                </div>

                {view === "choice"
                  ? Object.entries(question?.options ?? {}).map(([ol, text]) => (
                    <button key={ol} className={"rs-opt" + (st.answer === ol ? " on" : "")} onClick={() => pickAnswer(ref.question_id, ol)}>
                      <span className="ol">{ol}</span>{text}
                    </button>
                  ))
                  : null}

                <div className="rs-qtools">
                  <button className={"qtool" + (st.doubt ? " on" : "")} onClick={() => doDoubt(ref.question_id)}>
                    <svg className="ic" viewBox="0 0 24 24"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" /><line x1="4" x2="4" y1="22" y2="15" /></svg>
                    {st.doubt ? "已标存疑" : "标存疑"}
                  </button>
                  {isAnswered(st) ? <button className="qtool" onClick={() => doClear(ref.question_id)}>清空本题</button> : null}
                  {st.excluded.length ? <span className="qhint">排除过 {st.excluded.join("、")}</span> : null}
                  {st.candidates.length ? <span className="qhint">暂定 {st.candidates.join("、")}</span> : null}
                </div>

                {myNotes.length ? (
                  <div className="qnotes">
                    {myNotes.map(n => (
                      <div className="qnote" key={n.id}>
                        <span className={"qn-k " + n.kind}>{n.kind === "word" ? "生词" : n.kind === "line" ? "出题句" : "想法"}</span>
                        <span className="qn-t">{n.body}</span>
                        {n.anchor ? <span className="qn-q">“{n.anchor.quote}”</span> : <span className="qn-q dim">（没记原句）</span>}
                        <button className="note-x" onClick={() => removeNote(n.id)}>×</button>
                      </div>
                    ))}
                  </div>
                ) : null}
                <NoteComposer
                  value={noteText}
                  open={activeId === ref.question_id && noteDraft}
                  onChange={setNoteText}
                  onOpen={() => { setActiveId(ref.question_id); setNoteDraft(true); }}
                  onCancel={() => setNoteDraft(false)}
                  onSave={confirmNote}
                />
              </div>
            );
          })}
        </div>
      </div>

      {sel && tool === "cursor" ? (
        <div className="selbar rs-selbar" style={{ left: sel.x, top: sel.y - 46 }} onMouseDown={e => e.stopPropagation()}>
          {aidsOk ? <button onClick={() => doLookup(sel.quote)}><svg className="ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></svg>查词</button> : null}
          {aidsOk && /^[A-Za-z'’-]+$/.test(sel.quote) ? (
            <button onClick={() => { const a = new Audio(audioUrl(sel.quote, "us")); void a.play(); }}><svg className="ic" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4Z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /></svg>朗读</button>
          ) : null}
          <button className="tool-aid disabled" title="翻译这句还没接好，暂不可用" disabled>翻译这句（暂未接入）</button>
          <span className="sep" />
          <button onClick={doLine}><svg className="ic" viewBox="0 0 24 24"><path d="m9 11-6 6v3h9l3-3" /><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4" /></svg>作为第 {refById(structure, activeId)?.number ?? "?"} 题依据</button>
          <button onClick={() => setNoteDraft(true)}><svg className="ic" viewBox="0 0 24 24"><path d="M15.5 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V8.5Z" /><path d="M15 3v6h6" /></svg>写便签</button>
        </div>
      ) : null}

      <div className="annot-dock rs-dock">
        <span className="ad-label">批注</span>
        <button className={tool === "cursor" ? "on" : ""} title="光标 / 做题" onClick={() => setTool("cursor")}>{CURSOR}</button>
        <button className={tool === "pen" ? "on" : ""} title="画笔" onClick={() => setTool("pen")}>{PEN}</button>
        <button className={tool === "eraser" ? "on" : ""} title="橡皮" onClick={() => setTool("eraser")}>{ERASER}</button>
        <button title="清空笔迹" onClick={() => window.dispatchEvent(new CustomEvent("rs:clear-annot"))}>{CLEAR}</button>
        {tool === "pen" ? (
          <span className="rs-penopt">
            {["#c2402a", "#2f5fa8", "#1d2025"].map(c => (
              <span key={c} className={"ad-dot" + (penColor === c ? " on" : "")} style={{ ["--dot" as string]: c }} onClick={() => setPenColor(c)}><i /></span>
            ))}
            {[1.5, 2.5, 4].map(w => (
              <span key={w} className={"ad-w" + (penWidth === w ? " on" : "")} onClick={() => setPenWidth(w)}><i style={{ width: 11, height: w }} /></span>
            ))}
          </span>
        ) : null}
      </div>

      {notesOpen ? (
        <div className="notes-panel open">
          <div className="notes-head"><h4>这次作答的标记</h4><button className="iconbtn" onClick={() => setNotesOpen(false)}>×</button></div>
          <div className="notes-tip">做题时 Glyph 不会插话；这些只在你点「让 Glyph 讲这题」时给它看。</div>
          <div className="notes-list">
            {attempt.notes.length === 0 ? <div className="notes-empty">还没有标记。选中原文可以查词 / 划线 / 写想法。</div> : null}
            {attempt.notes.map(n => (
              <div className="note-i" key={n.id}>
                <div className={"note-ic " + n.kind}>{n.kind === "word" ? "A" : n.kind === "line" ? "—" : "✎"}</div>
                <div className="note-main">
                  <div className="note-k">
                    {NOTE_TITLE[n.kind]} · {n.question_id
                      ? `第 ${refById(structure, n.question_id)?.number ?? "?"} 题`
                      : "整篇"}
                  </div>
                  <div className="note-t">{n.body}{n.anchor ? <span className="note-d">　“{n.anchor.quote}”</span> : null}</div>
                </div>
                <button className="note-x" onClick={() => removeNote(n.id)}>×</button>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <button className="notes-fab" onClick={() => setNotesOpen(o => !o)}>
        <svg className="ic" viewBox="0 0 24 24"><path d="M2 6h4" /><path d="M2 10h4" /><path d="M2 14h4" /><path d="M2 18h4" /><rect x="6" y="3" width="16" height="18" rx="2" /></svg>
        我的标记 · 笔记 <span className="cnt">{attempt.notes.length}</span>
      </button>

      {pendingMove ? (
        <div className="timesup open">
          <div className="ts-box">
            <div className="ts-ic">{CLOCK}</div>
            <h5>这个句子已经用在第 {pendingMove.from} 题了</h5>
            <p>把它挪到第 {refById(structure, pendingMove.question_id)?.number ?? ""} 题，原来那一空会被清空。</p>
            <div className="ts-btns">
              <button className="btn ghost sm" onClick={() => setPendingMove(null)}>取消</button>
              <button className="btn primary sm" onClick={() => {
                const { states } = moveGapOption(attempt.questions, pendingMove.question_id, pendingMove.letter);
                const changed = [pendingMove.question_id];
                void onPatch({ ...attempt, questions: states }, { questions: states.filter(q => changed.includes(q.question_id) || q.answer === "") });
                setPendingMove(null);
              }}>确认挪过来</button>
            </div>
          </div>
        </div>
      ) : null}

      {confirmSubmit ? (
        <div className="timesup open">
          <div className="ts-box">
            <div className="ts-ic">{CLOCK}</div>
            <h5>还有没答的题</h5>
            <p>第 {confirmSubmit.join("、")} 题还没作答。可以按漏答交卷，也可以回去补。</p>
            <div className="ts-btns">
              <button className="btn ghost sm" onClick={() => setConfirmSubmit(null)}>回去检查</button>
              <button className="btn primary sm" onClick={() => { setConfirmSubmit(null); onSubmit(); }}>按漏答交卷</button>
            </div>
          </div>
        </div>
      ) : null}

      {toast ? <div className="toast show">{toast}</div> : null}
    </div>
  );
}

const HEAD_TAG: Record<string, string> = {
  choice: "PASSAGE",
  matching: "PASSAGE",
  gap: "PASSAGE WITH GAPS",
};
const NOTE_TITLE: Record<string, string> = { word: "生词", line: "自划出题句", note: "便签" };

function NoteComposer({ value, open, onChange, onOpen, onCancel, onSave }: {
  value: string;
  open: boolean;
  onChange: (v: string) => void;
  onOpen: () => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (!open) {
    return (
      <button className="qtool" onClick={onOpen}>记一句想法</button>
    );
  }
  return (
    <span className="rs-notedraft" onClick={e => e.stopPropagation()}>
      <input
        autoFocus
        value={value}
        onChange={e => onChange(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter") onSave(); if (e.key === "Escape") onCancel(); }}
        placeholder="这一步我是怎么想的…"
      />
      <button onClick={onSave}>保存</button>
    </span>
  );
}

/* ---------- 手绘批注：一张 canvas 覆在原文栏上，笔迹留在本地 ---------- */
function AnnotLayer({ quizId, tool, color, width }: { quizId: string; tool: Tool; color: string; width: number }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const drawing = useRef(false);
  const key = `rs-annot-${quizId}`;

  useEffect(() => {
    const cv = canvasRef.current;
    const parent = cv?.parentElement;
    if (!cv || !parent) return;
    const fit = () => {
      const w = parent.scrollWidth || parent.clientWidth;
      const h = parent.scrollHeight || parent.clientHeight;
      if (cv.width === w && cv.height === h) return;
      const old = document.createElement("canvas");
      old.width = cv.width;
      old.height = cv.height;
      old.getContext("2d")?.drawImage(cv, 0, 0);
      cv.width = w;
      cv.height = h;
      cv.getContext("2d")?.drawImage(old, 0, 0);
    };
    try {
      const data = localStorage.getItem(key);
      if (data) {
        const img = new Image();
        img.onload = () => cv.getContext("2d")?.drawImage(img, 0, 0, cv.width, cv.height);
        img.src = data;
      }
    } catch { /* localStorage 不可用时笔迹只在本次会话 */ }
    fit();
    const ro = new ResizeObserver(() => fit());
    ro.observe(parent);
    return () => ro.disconnect();
  }, [key, canvasRef]);

  useEffect(() => {
    const clear = () => {
      const cv = canvasRef.current;
      if (!cv) return;
      cv.getContext("2d")?.clearRect(0, 0, cv.width, cv.height);
      try { localStorage.removeItem(key); } catch { /* ignore */ }
    };
    window.addEventListener("rs:clear-annot", clear);
    return () => window.removeEventListener("rs:clear-annot", clear);
  }, [key, canvasRef]);

  const toLocal = (e: PointerEvent) => {
    const cv = canvasRef.current;
    if (!cv) return { x: 0, y: 0 };
    const r = cv.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * cv.width, y: ((e.clientY - r.top) / r.height) * cv.height };
  };

  const down = (e: PointerEvent) => {
    if (tool === "cursor") return;
    drawing.current = true;
    const cv = canvasRef.current;
    const ctx = cv?.getContext("2d");
    if (!cv || !ctx) return;
    const p = toLocal(e);
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    if (tool === "eraser") {
      ctx.globalCompositeOperation = "destination-out";
      ctx.lineWidth = width * 5;
    } else {
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
    }
  };
  const move = (e: PointerEvent) => {
    if (!drawing.current) return;
    const ctx = canvasRef.current?.getContext("2d");
    if (!ctx) return;
    const p = toLocal(e);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
  };
  const up = () => {
    if (!drawing.current) return;
    drawing.current = false;
    const cv = canvasRef.current;
    cv?.getContext("2d")?.restore();
    try { localStorage.setItem(key, cv!.toDataURL()); } catch { /* ignore */ }
  };

  return (
    <canvas
      ref={canvasRef}
      className="annot-layer"
      style={{ pointerEvents: tool === "cursor" ? "none" : "auto", cursor: tool === "pen" ? "crosshair" : tool === "eraser" ? "cell" : undefined }}
      onPointerDown={down}
      onPointerMove={move}
      onPointerUp={up}
      onPointerLeave={up}
    />
  );
}
