import { useMemo, useState } from "react";
import {
  anchorStatus,
  buildExplainPrompt,
  buildReviewItems,
  elapsedLabel,
  mapJudgeToReview,
  explainLabel,
  refById,
  renderSegments,
  resultOf,
  stateOf,
  VIEW_TITLE,
  type PassageDetail,
  type ReadingAttempt,
  type RenderMark,
} from "./reading";

const CHECK = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
);
const CROSS = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
);
const COPY = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
);

export default function Review({
  detail,
  attempt,
  onBack,
  onRedo,
}: {
  detail: PassageDetail;
  attempt: ReadingAttempt;
  onBack: () => void;
  onRedo: () => void;
}) {
  const structure = detail.structure;
  const items = useMemo(() => buildReviewItems(detail, attempt), [detail, attempt]);
  const [filter, setFilter] = useState<"all" | "worth">("all");
  const [open, setOpen] = useState<string[]>(() => items.filter(i => !i.result?.correct).map(i => i.question_id));
  const [copied, setCopied] = useState("");

  const results = mapJudgeToReview(attempt.results, detail.questions);
  const right = results.filter(r => r.correct).length;
  const wrong = results.filter(r => !r.missed && !r.correct).length;
  const missed = results.filter(r => r.missed).length;

  const visible = filter === "worth" ? items.filter(i => i.needsReview) : items;

  return (
    <div className="rs-workspace rs-review">
      <header className="rs-head">
        <button className="rs-back" onClick={onBack}>← 题库</button>
        <div>
          <div className="rs-htitle">复盘 · {VIEW_TITLE[structure.view] ?? detail.qtype}</div>
          <div className="rs-hsub">
            {[detail.exam, detail.year ? `${detail.year}年` : "", detail.source].filter(Boolean).join(" · ")}
            {attempt.submitted_at ? ` · ${elapsedLabel(attempt)}` : ""}
          </div>
        </div>
        <div className="rs-hsp" />
        <div className="rs-prog">
          <span className="g">对 {right}</span> · <span className="r">错 {wrong}</span> · <span className="m">漏 {missed}</span>
        </div>
        <div className="rv-filter">
          <button className={filter === "all" ? "on" : ""} onClick={() => setFilter("all")}>全部 {items.length} 题</button>
          <button className={filter === "worth" ? "on" : ""} onClick={() => setFilter("worth")}>值得回看 {items.filter(i => i.needsReview).length} 题</button>
        </div>
        <button className="btn ghost sm" onClick={onRedo}>重新做一遍</button>
      </header>

      <div className="rs-grid">
        <div className="rs-col rs-passage">
          <div className="rs-phead">
            <span className="tag">{structure.view === "matching" ? "PASSAGE" : "PASSAGE"}</span>
            <span className="ttl">你的划线 / 生词标记原样保留，说对了才讲</span>
          </div>
          <div className="rs-passage-inner">
            {structure.blocks.map(b => {
              if (b.kind === "blank") {
                const st = stateOf(attempt, b.question_id ?? "");
                const no = refById(structure, b.question_id ?? "")?.number ?? b.blank_no ?? 0;
                const result = resultOf(attempt, b.question_id ?? "");
                return (
                  <span key={b.id} className={"gap-slot filled" + (result && !result.correct ? " bad" : "")}>
                    {no}. {structure.options[st.answer] ?? (st.answer || "（没填）")}
                  </span>
                );
              }
              return (
                <p className={"rs-p" + (structure.view === "matching" ? " matched" : "")} key={b.id} data-block={b.id}>
                  {b.label ? <span className="pn">{b.label}</span> : null}
                  {renderSegments(b.text, notesMarks(attempt, b.id)).map((s, j) =>
                    s.kind === "word" ? <span className="w-word" key={j}>{s.t}</span>
                      : s.kind === "line" ? <span className="w-line" key={j}>{s.t}</span>
                        : <span key={j}>{s.t}</span>,
                  )}
                </p>
              );
            })}
          </div>
        </div>

        <div className="rs-col rs-questions">
          {visible.map(item => {
            const r = item.result;
            const st = item.state;
            const notes = item.notes;
            const opened = open.includes(item.question_id);
            const ref = refById(structure, item.question_id);
            return (
              <div className={"rs-q locked" + (r?.correct ? " ok" : " bad")} key={item.question_id}>
                <div className="rs-qtop">
                  <span className="rs-qno">{item.number}</span>
                  <div className="rs-qstem">{item.stem}</div>
                  {item.missed ? (
                    <span className="qverdict miss">— 未作答</span>
                  ) : r?.correct ? (
                    <span className="qverdict ok">{CHECK} 答对</span>
                  ) : (
                    <span className="qverdict bad">{CROSS} 答错</span>
                  )}
                </div>

                {structure.view === "gap" ? (
                  <div className="ansline">
                    {r && !r.correct && r.your_answer ? <span className="you bad">你填 <b>{r.your_answer}</b></span> : null}
                    <span className="right">正确 <b>{r?.correct_answer ?? "—"}</b></span>
                  </div>
                ) : structure.view === "matching" ? (
                  <div className="ansline">
                    {r && !r.correct && r.your_answer ? <span className="you bad">你配 <b>{r.your_answer}</b></span> : null}
                    <span className="right">正确 <b>{r?.correct_answer ?? "—"}</b></span>
                  </div>
                ) : (
                  Object.entries(item.options).map(([ol, text]) => {
                    const isRight = ol === r?.correct_answer;
                    const isWrongPick = r?.your_answer === ol && ol !== r?.correct_answer;
                    return (
                      <button key={ol} className={"rs-opt" + (isRight ? " correct" : "") + (isWrongPick ? " wrongpick" : "")} disabled>
                        <span className="ol">{ol}</span>{text}
                      </button>
                    );
                  })
                )}

                <div className="rv-meta">
                  {st.doubt ? <span className="qhint">我自己标了存疑</span> : null}
                  {st.excluded.length ? <span className="qhint">排除过 {st.excluded.join("、")}</span> : null}
                  {st.candidates.length ? <span className="qhint">暂定过 {st.candidates.join("、")}</span> : null}
                  {attempt.aids_used.length ? <span className="qhint">用过辅助：{attempt.aids_used.join("、")}</span> : null}
                </div>

                {notes.length ? (
                  <div className="qnotes">
                    {notes.map(n => {
                      const status = anchorStatus(
                        n.anchor,
                        structure.blocks.find(b => b.id === n.anchor?.block_id)?.text,
                        attempt.passage_revision,
                      );
                      return (
                        <div className="qnote" key={n.id}>
                          <span className={"qn-k " + n.kind}>{n.kind === "word" ? "生词" : n.kind === "line" ? "出题句" : "想法"}</span>
                          <span className="qn-t">{n.body}</span>
                          {n.anchor ? (
                            status === "ok"
                              ? <span className="qn-q">“{n.anchor.quote}”</span>
                              : <span className="qn-q stale">原句待核对（题卡有改动）</span>
                          ) : <span className="qn-q dim">（没记原句）</span>}
                        </div>
                      );
                    })}
                  </div>
                ) : null}

                <div className="rv-acts">
                  <button className={"qtool primaryish" + (opened ? " on" : "")} onClick={() => setOpen(o => (o.includes(item.question_id) ? o.filter(x => x !== item.question_id) : [...o, item.question_id]))}>
                    {explainLabel(item)}
                  </button>
                  {ref?.blank_no ? <span className="qhint">原题空号 {ref.blank_no}</span> : null}
                </div>

                {opened ? (
                  <div className="rv">
                    <div className="rv-ava">M</div>
                    <div className="rv-body">
                      <div className="rv-h">先把这次作答的事实摆出来</div>
                      <ul className="rv-facts">
                        <li>你的作答：{item.missed ? "漏答" : `${st.answer}${r?.correct ? "（答对）" : "（答错）"}`}</li>
                        <li>标准答案：{r?.correct_answer ?? "—"}（来自题库，死数据比对）</li>
                        {notes.length ? <li>你当时记的：{notes.map(n => n.body).join(" / ")}</li> : null}
                        {notes.some(n => n.anchor) ? <li>你标出的原句：{notes.filter(n => n.anchor).map(n => `“${n.anchor!.quote}”`).join("　")}</li> : null}
                      </ul>
                      <p className="rv-warn">
                        这一步还不接真模型：想要由 Glyph 来讲，
                        <b>Agent 讲题工具（explain_reading_question）在下一阶段</b>，
                        届时这里直接显示讲稿。现在可以先复制下面的上下文去对话里问。
                      </p>
                      <div className="rv-acts">
                        <button className="qtool" onClick={() => {
                          void navigator.clipboard?.writeText(buildExplainPrompt({ attempt, detail, question_id: item.question_id }))
                            .then(() => setCopied(item.question_id))
                            .catch(() => setCopied(""));
                        }}>{COPY} 复制讲题上下文</button>
                        {copied === item.question_id ? <span className="qhint">已复制</span> : null}
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>

      <div className="rv-savebar">
        <span>这次作答已经存好，退出后回到题库还能找回这一份复盘。</span>
        <span className="sp" />
        <button className="btn ghost sm" onClick={onBack}>回题库</button>
        <button className="btn primary sm" onClick={onRedo}>重新做一遍</button>
      </div>
    </div>
  );
}

function notesMarks(attempt: ReadingAttempt, blockId: string): RenderMark[] {
  return attempt.notes
    .filter(n => n.anchor && n.anchor.block_id === blockId)
    .map(n => ({ start: n.anchor!.start, end: n.anchor!.end, kind: n.kind === "word" ? "word" : "line" }));
}
