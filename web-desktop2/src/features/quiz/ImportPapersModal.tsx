import { useEffect, useRef, useState } from "react";
import {
  BATCH_MAX_PAPERS,
  INTAKE_ACCEPT,
  INTAKE_EXAMS,
  buildBatchForm,
  fileSupported,
  formatSize,
  guessPaperMeta,
  importPaper,
  pairIntoSlots,
  qtypesForExam,
  submitBatchForm,
  type BatchSlot,
  type BatchOutcome,
  type ImportOutcome,
  type IntakeForm,
} from "./quizImport";
import { QTYPE_ORDER } from "./quizLibrary";

type Phase = "form" | "working" | "done" | "empty";
type Mode = "single" | "batch";
type Meta = { exam: string; year: string; setNo: string };

type SingleOk = Extract<ImportOutcome, { ok: true }>;
type BatchOk = Extract<BatchOutcome, { ok: true }>;

const EMPTY_META: Meta = { exam: "", year: "", setNo: "" };

const DOC_ICON = (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>
);
const ANS_ICON = (
  <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="m9 13 2 2 4-4"/></svg>
);
const PLUS_ICON = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
);

function storeLabel(meta: Meta): string {
  return [meta.exam.trim(), meta.year.trim() ? `${meta.year.trim()} 年` : "", meta.setNo.trim() ? `第 ${meta.setNo.trim()} 套` : ""]
    .filter(Boolean).join(" · ");
}

/**
 * 导入真题弹窗：一套 / 多套（最多 3）两种走法。
 * 一套：一个真题文件 + 「答案在哪」二选一（在卷子里 / 单独答案），也可以直接粘贴文字；
 * 多套：考试年份只填一次，下面逐套列槽，每套可单独给答案，某套失败不拖累其它套。
 * 拆题全程在本机后端完成，不联网、不调模型。
 */
export default function ImportPapersModal({
  open, onClose, onImported, onPractice,
}: {
  open: boolean;
  onClose: () => void;
  onImported: () => void;
  onPractice: (prompt: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("single");
  const [phase, setPhase] = useState<Phase>("form");

  // 一套
  const [qFile, setQFile] = useState<File | null>(null);
  const [aFile, setAFile] = useState<File | null>(null);
  const [ansSep, setAnsSep] = useState(false);
  const [meta, setMeta] = useState<Meta>(EMPTY_META);
  const [guessHint, setGuessHint] = useState(false);
  const [paste, setPaste] = useState(false);
  const [qText, setQText] = useState("");
  const [aText, setAText] = useState("");

  // 多套
  const [bExam, setBExam] = useState("");
  const [bYear, setBYear] = useState("");
  const [slots, setSlots] = useState<BatchSlot[]>([]);
  const ansPickRef = useRef<number>(-1);

  const [single, setSingle] = useState<SingleOk | null>(null);
  const [batch, setBatch] = useState<BatchOk | null>(null);
  const [errMsg, setErrMsg] = useState("");
  const [notice, setNotice] = useState("");

  const qRef = useRef<HTMLInputElement>(null);
  const aRef = useRef<HTMLInputElement>(null);
  const slotRef = useRef<HTMLInputElement>(null);
  const slotAnsRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setMode("single"); setPhase("form");
    setQFile(null); setAFile(null); setAnsSep(false); setMeta(EMPTY_META);
    setGuessHint(false); setPaste(false); setQText(""); setAText("");
    setBExam(""); setBYear(""); setSlots([]);
    setSingle(null); setBatch(null); setErrMsg(""); setNotice("");
  }, [open]);

  if (!open) return null;

  const working = phase === "working";
  const patchMeta = (p: Partial<Meta>) => setMeta(m => ({ ...m, ...p }));

  // ---------- 一套：选文件 / 识别 / 提交 ----------
  const pickQuestion = (files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    if (!fileSupported(f.name)) {
      setNotice(`「${f.name}」收不了——支持 PDF / Word(.docx) / txt，老 .doc 先另存为 .docx。`);
      return;
    }
    setNotice("");
    setQFile(f);
    const g = guessPaperMeta(f.name);
    setMeta(m => ({
      exam: m.exam || g.exam || "",
      year: m.year || g.year || "",
      setNo: m.setNo || (g.setNo ? String(g.setNo) : ""),
    }));
    setGuessHint(!!(g.exam || g.year || g.setNo));
  };

  const pickAnswer = (files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    if (!fileSupported(f.name)) {
      setNotice(`「${f.name}」收不了——支持 PDF / Word(.docx) / txt。`);
      return;
    }
    setNotice("");
    setAFile(f);
  };

  const singleReady = paste
    ? meta.exam.trim() !== "" && qText.trim() !== "" && aText.trim() !== ""
    : meta.exam.trim() !== "" && !!qFile && (!ansSep || !!aFile);

  const singleLack = () => {
    if (!meta.exam.trim()) return "先选一下是哪场考试";
    if (paste) return qText.trim() && aText.trim() ? `将存进「${storeLabel(meta)}」` : "把题目和答案文字都贴进来";
    if (!qFile) return "把真题文件拖进来";
    if (ansSep && !aFile) return "还差「答案 / 解析」就能导入";
    return `将存进「${storeLabel(meta)}」`;
  };

  const submitSingle = async () => {
    if (!singleReady || working) return;
    setPhase("working"); setErrMsg("");
    const form: IntakeForm = {
      exam: meta.exam.trim(),
      year: meta.year.trim(),
      source: meta.setNo.trim() ? `第${meta.setNo.trim()}套` : "",
      questions: { file: paste ? null : qFile, text: paste ? qText : "" },
      answers: {
        file: !paste && ansSep ? aFile : null,
        text: paste ? aText : "",
      },
    };
    const out = await importPaper(form);
    if (!out.ok) { setErrMsg(out.error); setPhase("form"); return; }
    setSingle(out);
    setPhase(out.count > 0 ? "done" : "empty");
    if (out.count > 0) onImported();
  };

  // ---------- 多套：槽位 / 提交 ----------
  const addSlotFiles = (files: FileList | null) => {
    const picked = Array.from(files ?? []);
    if (picked.length === 0) return;
    const bad = picked.filter(f => !fileSupported(f.name));
    const accepted = picked.filter(f => fileSupported(f.name));

    // 已在槽里的卷子 + 已配答案摊平，和新文件合在一起，按文件名重新配成「一套=卷子+可选答案」。
    // 上限按套数（最多 3 套），题答分开放时一套是两个文件，不能按文件数数到 3 就丢。
    const existing = slots.flatMap(s =>
      s.answerFile ? [s.questionFile, s.answerFile] : [s.questionFile]);
    const seen = new Set<string>();
    const all: File[] = [];
    for (const f of [...existing, ...accepted]) {
      const k = `${f.name}|${f.size}`;
      if (seen.has(k)) continue;
      seen.add(k);
      all.push(f);
    }

    const hadSlots = slots.length > 0;
    const { slots: next, orphanAnswers, droppedPapers } = pairIntoSlots(all, BATCH_MAX_PAPERS);
    setSlots(next);

    // 第一次配成套时，共享考试 / 年份还空着就用第一个卷子文件名认一版
    if (!hadSlots && next.length > 0) {
      const g = guessPaperMeta(next[0].questionFile.name);
      if (g.exam && !bExam) setBExam(g.exam);
      if (g.year && !bYear) setBYear(g.year);
    }

    const msgs: string[] = [];
    if (bad.length > 0) {
      msgs.push(`「${bad.map(f => f.name).join("、")}」收不了——支持 PDF / Word(.docx) / txt，老 .doc 先另存为 .docx。`);
    }
    if (droppedPapers.length > 0) {
      msgs.push(`一次最多 ${BATCH_MAX_PAPERS} 套，「${droppedPapers.map(f => f.name).join("、")}」这批先不收，导完这批再导下一批。`);
    }
    if (orphanAnswers.length > 0) {
      msgs.push(`「${orphanAnswers.map(f => f.name).join("、")}」像答案文件，没找到对应的卷子——把这套卷子也一起拖进来。`);
    }
    setNotice(msgs.join(" "));
  };

  const removeSlot = (i: number) => setSlots(prev => prev.filter((_, idx) => idx !== i));

  const pickSlotAnswer = (i: number, files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    if (!fileSupported(f.name)) { setNotice(`「${f.name}」收不了。`); return; }
    setSlots(prev => prev.map((s, idx) => (idx === i ? { ...s, answerFile: f } : s)));
  };

  const slotSetLabel = (s: BatchSlot, i: number): string => {
    const g = guessPaperMeta(s.questionFile.name).setNo;
    return `第 ${g ?? i + 1} 套`;
  };

  const batchReady = bExam.trim() !== "" && slots.length > 0;

  const submitBatch = async () => {
    if (!batchReady || working) return;
    setPhase("working"); setErrMsg("");
    const fd = buildBatchForm({ exam: bExam, year: bYear }, slots);
    const out = await submitBatchForm(fd);
    if (!out.ok) { setErrMsg(out.error); setPhase("form"); return; }
    setBatch(out);
    setPhase("done");
    if (out.count > 0) onImported();
  };

  const retrySlot = (i: number) => {
    const s = slots[i];
    if (!s) return;
    const g = guessPaperMeta(s.questionFile.name);
    setMode("single"); setPhase("form"); setBatch(null); setErrMsg("");
    setPaste(false);
    setQFile(s.questionFile);
    setAFile(s.answerFile);
    setAnsSep(!!s.answerFile);
    setMeta({
      exam: bExam || g.exam || "",
      year: bYear || g.year || "",
      setNo: g.setNo ? String(g.setNo) : String(i + 1),
    });
    setGuessHint(false);
  };

  const resetSingle = () => {
    setQFile(null); setAFile(null); setAnsSep(false); setPaste(false);
    setQText(""); setAText(""); setSingle(null); setErrMsg(""); setNotice("");
    setPhase("form");
  };
  const resetBatch = () => {
    setSlots([]); setBatch(null); setErrMsg(""); setNotice(""); setPhase("form");
  };

  const practicePrompt = `来道${(mode === "single" ? meta.exam : bExam).trim() || "这套"}仔细阅读`;
  const goPractice = () => onPractice(practicePrompt);

  // ---------- 弹窗宽度 ----------
  const width = phase === "working" ? 420
    : phase === "empty" ? 500
    : phase === "done" ? (mode === "batch" ? 520 : 480)
    : mode === "batch" ? 620 : 600;

  return (
    <div className="set-mask open" onMouseDown={e => { if (e.target === e.currentTarget && !working) onClose(); }}>
      <div className="set-modal qi-modal" style={{ width }} role="dialog" aria-label="导入真题">
        {phase !== "working" && (
          <div className="set-head">
            {phase === "form" ? (
              <div className="qi-title">
                <h3>导入真题</h3>
                <span className="qi-segmini">
                  <button type="button" className={mode === "single" ? "on" : ""} onClick={() => setMode("single")}>一套</button>
                  <button type="button" className={mode === "batch" ? "on" : ""} onClick={() => setMode("batch")}>多套 · 最多 {BATCH_MAX_PAPERS}</button>
                </span>
              </div>
            ) : <h3>{mode === "batch" ? "导入结果" : "导入真题"}</h3>}
            <button type="button" className="set-x" onClick={onClose} aria-label="关闭" disabled={working}>✕</button>
          </div>
        )}

        {phase === "working" ? (
          <div className="set-body qi-work-wrap">
            <Working />
          </div>
        ) : phase === "done" && mode === "single" && single ? (
          <SingleResult
            r={single} meta={meta}
            onAgain={resetSingle} onPractice={goPractice}
          />
        ) : phase === "empty" ? (
          <EmptyResult onPaste={() => { setPaste(true); setPhase("form"); }} onBack={() => setPhase("form")} />
        ) : phase === "done" && mode === "batch" && batch ? (
          <BatchResult r={batch} exam={bExam} year={bYear} slots={slots}
            onRetry={retrySlot} onAgain={resetBatch} onPractice={goPractice} />
        ) : mode === "single" ? (
          <>
            <div className="set-body">
              <p className="qi-sub">把真题（连同答案）拖进来，自动拆成{qtypesForExam(meta.exam).map(q => q.value).join("、")}这几块题卡。只在你的电脑上处理，不上传。</p>

              {paste ? (
                <div className="qi-paste-grid">
                  <label className="qi-paste-k">题目文字
                    <textarea className="qi-paste" value={qText} disabled={working}
                      placeholder="把题目整段贴进来（带 Section 开头的说明更好）"
                      onChange={e => setQText(e.target.value)} />
                  </label>
                  <label className="qi-paste-k">答案 / 解析文字
                    <textarea className="qi-paste" value={aText} disabled={working}
                      placeholder="把答案 / 解析整段贴进来，题号和答案字母在里面就行"
                      onChange={e => setAText(e.target.value)} />
                  </label>
                  <button type="button" className="qi-link" onClick={() => setPaste(false)}>改用文件导入</button>
                </div>
              ) : (
                <>
                  <DropZone file={qFile} variant="main"
                    title="放真题文件（题目和答案在同一份也行）"
                    sub="拖进来或点选 · PDF / Word(.docx) / txt"
                    filledHint={ansSep ? "真题文件" : "题目和答案都在这份里"}
                    onPick={pickQuestion} onActivate={() => qRef.current?.click()} />
                  <input ref={qRef} type="file" accept={INTAKE_ACCEPT} hidden
                    onChange={e => { pickQuestion(e.target.files); e.target.value = ""; }} />

                  {guessHint && qFile ? (
                    <p className="qi-guessok">已按文件名认出考试 / 年份 / 套次，不对可在下面直接改。</p>
                  ) : null}

                  <MetaFields meta={meta} onPatch={patchMeta} />

                  <div className="qi-field-label">答案在哪？</div>
                  <div className="qi-seg">
                    <button type="button" className={"qi-segopt" + (!ansSep ? " on" : "")} onClick={() => setAnsSep(false)}>
                      <span className="qi-sot"><span className="qi-sodot" />答案就在这份卷子里</span>
                      <span className="qi-sos">卷末答案页 / 题后解析，最省事</span>
                    </button>
                    <button type="button" className={"qi-segopt" + (ansSep ? " on" : "")} onClick={() => setAnsSep(true)}>
                      <span className="qi-sot"><span className="qi-sodot" />我有单独的答案文件</span>
                      <span className="qi-sos">再传一份答案 / 解析</span>
                    </button>
                  </div>

                  {ansSep ? (
                    <>
                      <DropZone file={aFile} variant="answer"
                        title="放答案 / 解析文件"
                        sub="拖进来或点选 · PDF / Word / txt"
                        filledHint="单独的答案 / 解析"
                        onPick={pickAnswer} onActivate={() => aRef.current?.click()} />
                      <input ref={aRef} type="file" accept={INTAKE_ACCEPT} hidden
                        onChange={e => { pickAnswer(e.target.files); e.target.value = ""; }} />
                    </>
                  ) : null}

                  <p className="qi-note">自动拆四块；答案对不上的题会跳过，听力、翻译这版暂不收。<button type="button" className="qi-link" onClick={() => setPaste(true)}>没有文件？粘贴文字</button></p>
                </>
              )}

              {notice ? <p className="set-msg err" role="alert">{notice}</p> : null}
              {errMsg ? <p className="set-msg err" role="alert">{errMsg}</p> : null}
            </div>
            <div className="su-foot">
              <span className="qi-lack">{singleLack()}</span>
              <button type="button" className="btn ghost sm" onClick={onClose} disabled={working}>取消</button>
              <button type="button" className="btn primary sm" disabled={!singleReady || working} onClick={submitSingle}>
                {working ? <><span className="spin sm" />正在拆题…</> : "导入真题"}
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="set-body">
              <p className="qi-sub">一次最多 {BATCH_MAX_PAPERS} 套，可一次拖入多个文件。考试、年份下面只填一次，套次按文件自动区分。</p>

              <SharedMeta exam={bExam} year={bYear} onExam={setBExam} onYear={setBYear} />

              <div className="qi-slots"
                onDragOver={e => { e.preventDefault(); }}
                onDrop={e => { e.preventDefault(); addSlotFiles(e.dataTransfer.files); }}>
                {slots.map((s, i) => (
                  <div className="qi-slot" key={`${s.questionFile.name}-${i}`}>
                    <span className="qi-sno">{i + 1}</span>
                    <span className="qi-smain">
                      <span className="qi-sfn" title={s.questionFile.name}>{s.questionFile.name}</span>
                      <span className="qi-smeta">
                        {formatSize(s.questionFile.size)} · {slotSetLabel(s, i)} ·
                        {s.answerFile ? (
                          <button type="button" className="qi-sans on" title="改成答案在卷子里"
                            onClick={() => setSlots(prev => prev.map((x, idx) => idx === i ? { ...x, answerFile: null } : x))}>
                            单独答案：{s.answerFile.name} ✕
                          </button>
                        ) : (
                          <button type="button" className="qi-sans" title="为这一套选单独的答案 / 解析文件"
                            onClick={() => { ansPickRef.current = i; slotAnsRef.current?.click(); }}>
                            答案在同一份（可改）
                          </button>
                        )}
                      </span>
                    </span>
                    <button type="button" className="qi-srm" title="移除这一套" onClick={() => removeSlot(i)}>✕</button>
                  </div>
                ))}
                {slots.length < BATCH_MAX_PAPERS ? (
                  <button type="button" className="qi-slot add" onClick={() => slotRef.current?.click()}
                    onDragOver={e => e.preventDefault()}
                    onDrop={e => { e.preventDefault(); addSlotFiles(e.dataTransfer.files); }}>
                    {PLUS_ICON}添加第 {slots.length + 1} 套（拖入或点选，可多选）
                  </button>
                ) : null}
                <input ref={slotRef} type="file" accept={INTAKE_ACCEPT} multiple hidden
                  onChange={e => { addSlotFiles(e.target.files); e.target.value = ""; }} />
                <input ref={slotAnsRef} type="file" accept={INTAKE_ACCEPT} hidden
                  onChange={e => { pickSlotAnswer(ansPickRef.current, e.target.files); e.target.value = ""; }} />
              </div>

              <p className="qi-note">一次最多 <b>{BATCH_MAX_PAPERS} 套</b>，第 {BATCH_MAX_PAPERS + 1} 个不收、可分批导。某套答案单独成册时，点该套的「答案在同一份」改成单独上传。</p>
              {notice ? <p className="set-msg err" role="alert">{notice}</p> : null}
              {errMsg ? <p className="set-msg err" role="alert">{errMsg}</p> : null}
            </div>
            <div className="su-foot">
              <span className="qi-lack">
                {slots.length === 0 ? "添加要导入的套卷"
                  : `已添加 ${slots.length} 套 · 还能加 ${Math.max(0, BATCH_MAX_PAPERS - slots.length)} 套`}
              </span>
              <button type="button" className="btn ghost sm" onClick={onClose} disabled={working}>取消</button>
              <button type="button" className="btn primary sm" disabled={!batchReady || working} onClick={submitBatch}>
                {working ? <><span className="spin sm" />正在拆题…</> : `导入 ${slots.length} 套真题`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ============================== 一套：小字段 / 拖放口 ============================== */

function MetaFields({ meta, onPatch }: { meta: Meta; onPatch: (p: Partial<Meta>) => void }) {
  return (
    <div className="qi-meta">
      <div className="qi-meta-exam">
        <div className="qi-chips">
          {INTAKE_EXAMS.map(c => (
            <button type="button" key={c} className={"pb-chip" + (meta.exam === c ? " on" : "")}
              onClick={() => onPatch({ exam: c })}>{c}</button>
          ))}
        </div>
        <input className="set-input qi-in" value={meta.exam} placeholder="考试（如 专八 / 雅思）"
          onChange={e => onPatch({ exam: e.target.value })} />
      </div>
      <input className="set-input qi-in" value={meta.year} placeholder="年份 如 2026"
        onChange={e => onPatch({ year: e.target.value.replace(/[^\d]/g, "").slice(0, 4) })} />
      <input className="set-input qi-in" value={meta.setNo} placeholder="套次 如 1"
        onChange={e => onPatch({ setNo: e.target.value.replace(/[^\d一二三四五六]/g, "").slice(0, 2) })} />
    </div>
  );
}

function SharedMeta({ exam, year, onExam, onYear }: {
  exam: string; year: string; onExam: (v: string) => void; onYear: (v: string) => void;
}) {
  return (
    <div className="qi-shared">
      <span className="qi-shared-label">考试 / 年份应用到下面全部套：</span>
      <div className="qi-chips">
        {INTAKE_EXAMS.map(c => (
          <button type="button" key={c} className={"pb-chip" + (exam === c ? " on" : "")}
            onClick={() => onExam(c)}>{c}</button>
        ))}
      </div>
      <input className="set-input qi-in qi-shared-exam" value={exam} placeholder="考试"
        onChange={e => onExam(e.target.value)} />
      <input className="set-input qi-in qi-shared-year" value={year} placeholder="年份 如 2026"
        onChange={e => onYear(e.target.value.replace(/[^\d]/g, "").slice(0, 4))} />
    </div>
  );
}

function DropZone({ file, variant, title, sub, filledHint, onPick, onActivate }: {
  file: File | null;
  variant: "main" | "answer";
  title: string;
  sub: string;
  filledHint: string;
  onPick: (files: FileList | null) => void;
  onActivate: () => void;
}) {
  const [over, setOver] = useState(false);
  if (file) {
    return (
      <button type="button" className={"qi-drop sel " + (variant === "answer" ? "answer" : "")}
        onClick={onActivate}>
        <span className="qi-dropic">{variant === "answer" ? ANS_ICON : DOC_ICON}</span>
        <span className="qi-dropmain">
          <span className="qi-fn" title={file.name}>{file.name}</span>
          <span className="qi-ds">{formatSize(file.size)} · {filledHint}</span>
        </span>
        <span className="qi-change">换一个</span>
      </button>
    );
  }
  return (
    <button type="button"
      className={"qi-drop " + (variant === "answer" ? "answer" : "") + (over ? " over" : "")}
      onClick={onActivate}
      onDragOver={e => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={e => { e.preventDefault(); setOver(false); onPick(e.dataTransfer.files); }}>
      <span className="qi-dropic">{variant === "answer" ? ANS_ICON : DOC_ICON}</span>
      <span className="qi-dt">{title}</span>
      <span className="qi-ds">{sub}</span>
    </button>
  );
}

/* ============================== 处理中 / 结果 / 失败 ============================== */

function Working() {
  return (
    <div className="qi-work">
      <span className="qi-spin" />
      <h4>正在读取文件、拆题…</h4>
      <span className="qi-bar"><i /></span>
      <p>只在你的电脑上进行，不联网、不调用模型。<br />PDF 越大越久，稍等一下。</p>
    </div>
  );
}

function qtypeRows(by: Record<string, string[]>) {
  return QTYPE_ORDER
    .map(t => ({ t, n: by[t]?.length ?? 0 }))
    .filter(x => x.n > 0);
}

function SingleResult({ r, meta, onAgain, onPractice }: {
  r: SingleOk; meta: Meta; onAgain: () => void; onPractice: () => void;
}) {
  const rows = qtypeRows(r.byQtype);
  const skipNote = () => {
    const parts: string[] = [];
    if (r.missing.length) parts.push(`「${r.missing.join("、")}」这套里没识别到`);
    parts.push("答案对不上的客观题已自动跳过；听力、翻译这版不收。");
    return parts.join("，");
  };
  return (
    <>
      <div className="set-body">
        <div className="qi-res">
          <div className="qi-restop">
            <span className="qi-resic ok"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 5 5L20 7"/></svg></span>
            <div>
              <h4>导入完成 · 拆出 {r.count} 篇题卡</h4>
              <p>已存进「{storeLabel(meta) || "本地题库"}」，以后随时在对话里喊它出题。</p>
            </div>
          </div>
          <div className="qi-resrows">
            {rows.map(x => (
              <div className="qi-resrow" key={x.t}><span className="k">{x.t}</span><span className="v">{x.n} 篇</span></div>
            ))}
          </div>
          <p className="qi-resskip">{skipNote()}</p>
          <div className="qi-restip">现在就试试，在对话里说 <span className="qi-tap">来道{meta.exam.trim() || "真题"}仔细阅读</span></div>
        </div>
      </div>
      <div className="su-foot">
        <button type="button" className="btn ghost sm" onClick={onAgain}>再导一套</button>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn primary sm" onClick={onPractice}>
          去练一道<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></svg>
        </button>
      </div>
    </>
  );
}

function EmptyResult({ onPaste, onBack }: { onPaste: () => void; onBack: () => void }) {
  return (
    <>
      <div className="set-body">
        <div className="qi-err">
          <div className="qi-errtop">
            <span className="qi-erric"><svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/></svg></span>
            <div>
              <h4>没从这份文件里拆出题来</h4>
              <p className="qi-esub">文件读到了，但没找到能收的四块题。最常见是下面三种情况，按顺序排查一下：</p>
            </div>
          </div>
          <div className="qi-errlist">
            <div className="qi-erritem"><span className="n">1</span><span><b>扫描 / 拍照版 PDF：</b>里面是图片、提取不到文字。换一份能选中、复制文字的版本，或直接把题目和答案文字粘贴进来。</span></div>
            <div className="qi-erritem"><span className="n">2</span><span><b>答案没对上：</b>卷末密密麻麻的「答案速查表」可能识别不全——改选「我有单独的答案文件」，传带逐题解析的那份；也顺手确认卷子和答案没传反。</span></div>
            <div className="qi-erritem"><span className="n">3</span><span><b>卷面和选的考试对不上：</b>不同考试的排版是两套拆法（四级一套、高考一套），上面「哪场考试」选错了就一块都拆不出；听力、翻译这两种不收。</span></div>
          </div>
        </div>
      </div>
      <div className="su-foot">
        <button type="button" className="btn ghost sm" onClick={onPaste}>直接粘贴文字</button>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn primary sm" onClick={onBack}>返回调整</button>
      </div>
    </>
  );
}

function BatchResult({ r, exam, year, slots, onRetry, onAgain, onPractice }: {
  r: BatchOk; exam: string; year: string; slots: BatchSlot[];
  onRetry: (i: number) => void; onAgain: () => void; onPractice: () => void;
}) {
  const okCount = r.papers.filter(p => !p.error).length;
  const failCount = r.papers.length - okCount;
  return (
    <>
      <div className="set-body">
        <div className="qi-bsummary">
          {failCount > 0 ? `${okCount} 套成功 · ${failCount} 套没拆出来` : `${okCount} 套全部导入完成`}
        </div>
        <div className="qi-brows">
          {r.papers.map((p, i) => {
            const g = slots[i] ? guessPaperMeta(slots[i].questionFile.name).setNo : undefined;
            const setNo = g ?? i + 1;
            const head = `第 ${setNo} 套 · ${exam.trim() || "真题"}${year.trim() ? ` ${year.trim()}` : ""}`;
            return p.error ? (
              <div className="qi-brow fail" key={`${p.key}-${i}`}>
                <span className="qi-bkic no"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/></svg></span>
                <span className="qi-bkmain">
                  <span className="qi-bkt">{head}</span>
                  <div className="qi-bks">没拆出题：可能是扫描 / 拍照版 PDF，或卷末答案没对上</div>
                </span>
                <button type="button" className="qi-bretry" onClick={() => onRetry(i)}>重新导这一套</button>
              </div>
            ) : (
              <div className="qi-brow" key={`${p.key}-${i}`}>
                <span className="qi-bkic ok"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12 5 5L20 7"/></svg></span>
                <span className="qi-bkmain">
                  <span className="qi-bkt">{head}</span>
                  <div className="qi-bks">
                    {qtypeRows(p.byQtype).map(x => `${x.t} ${x.n}`).join(" · ")} · 共 {p.count} 篇
                    {p.missing.length ? `（${p.missing.join("、")}没识别到）` : ""}
                  </div>
                </span>
              </div>
            );
          })}
        </div>
        {r.problems.length ? <p className="qi-resskip">{r.problems.join("；")}</p> : null}
        <div className="qi-restip">成功的已入库，现在可以在对话里说 <span className="qi-tap">来道{exam.trim() || "真题"}仔细阅读</span></div>
      </div>
      <div className="su-foot">
        <button type="button" className="btn ghost sm" onClick={onAgain}>再导一批</button>
        <span style={{ flex: 1 }} />
        <button type="button" className="btn primary sm" onClick={onPractice}>去练一道</button>
      </div>
    </>
  );
}
