import { useMemo, useState } from "react";
import { useAgent } from "../../agent/AgentContext";
import {
  extractQuiz,
  singleChoiceLetters,
  buildReadingAnswers,
  type ReadingQuestionView,
} from "./quizCard";

/**
 * get_quiz 出题后的作答卡（挂在时间线里，位置就是出题工具那一步）。
 * - 单题卡：题面整段展示（选项混排在文本里），下方给 A–H 快捷字母，点一下就把答案发出去；
 * - 阅读卡：文章 + 每题结构化选项，逐题点选后一次性提交 "1.D 2.A"。
 * 前端不判卷：答案作为普通消息发给 agent，由它调 check_quiz_answer 对题库标准答案。
 */
export default function QuizCard({ output }: { output: unknown }) {
  const t = useAgent();
  const quiz = useMemo(() => extractQuiz(output), [output]);
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [picks, setPicks] = useState<Record<number, string>>({});
  if (!quiz) return null;

  const locked = submitted !== null || t.running;
  const send = (text: string) => {
    if (locked) return;
    setSubmitted(text);
    void t.send(text, []);
  };

  return (
    <div className="qz-card">
      <div className="qz-head">
        <span className="qz-badge">题</span>
        <span className="qz-title">{quiz.qtype ? `练习题 · ${quiz.qtype}` : "练习题"}</span>
        <span className="qz-hint">点选作答，提交后我来对标准答案</span>
      </div>

      {quiz.kind === "single" ? (
        <SingleBody body={quiz.body} locked={locked} onPick={send} />
      ) : (
        <ReadingBody
          passage={quiz.passage}
          questions={quiz.questions}
          picks={picks}
          locked={locked}
          onPick={(n, letter) => !locked && setPicks((p) => ({ ...p, [n]: letter }))}
          onSubmit={() => {
            const ans = buildReadingAnswers(picks);
            if (ans) send(ans);
          }}
        />
      )}

      {submitted !== null ? (
        <div className="qz-done">已提交「{submitted}」，正在对标准答案…</div>
      ) : null}
    </div>
  );
}

function SingleBody({ body, locked, onPick }: {
  body: string;
  locked: boolean;
  onPick: (letter: string) => void;
}) {
  const letters = singleChoiceLetters(body);
  return (
    <div className="qz-single">
      <div className="qz-stem">{body}</div>
      <div className="qz-letters">
        {letters.map((l) => (
          <button key={l} className="qz-key" disabled={locked} onClick={() => onPick(l)}>{l}</button>
        ))}
      </div>
    </div>
  );
}

function ReadingBody({ passage, questions, picks, locked, onPick, onSubmit }: {
  passage: string;
  questions: ReadingQuestionView[];
  picks: Record<number, string>;
  locked: boolean;
  onPick: (n: number, letter: string) => void;
  onSubmit: () => void;
}) {
  const answered = questions.filter((q) => picks[q.number]).length;
  const allDone = answered === questions.length && questions.length > 0;
  return (
    <div className="qz-reading">
      <div className="qz-passage">{passage}</div>
      <div className="qz-qlist">
        {questions.map((q) => (
          <div key={q.number} className="qz-q">
            <div className="qz-qstem"><span className="qz-qno">{q.number}.</span> {q.stem}</div>
            <div className="qz-opts">
              {Object.entries(q.options)
                .sort(([a], [b]) => a.localeCompare(b))
                .map(([letter, text]) => {
                  const active = picks[q.number] === letter;
                  return (
                    <button
                      key={letter}
                      className={"qz-opt" + (active ? " active" : "")}
                      disabled={locked}
                      onClick={() => onPick(q.number, letter)}
                    >
                      <span className="qz-letter">{letter}</span>
                      <span className="qz-otext">{text}</span>
                    </button>
                  );
                })}
            </div>
          </div>
        ))}
      </div>
      <div className="qz-foot">
        <span className="qz-count">已答 {answered}/{questions.length}</span>
        <button className="qz-submit" disabled={locked || !allDone} onClick={onSubmit}>
          {allDone ? "提交答案" : `还有 ${questions.length - answered} 题没选`}
        </button>
      </div>
    </div>
  );
}
