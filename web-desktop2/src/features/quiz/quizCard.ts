/**
 * quiz 作答卡的纯逻辑层：把 get_quiz 工具结果归一化成题卡模型，并负责拼作答串。
 *
 * 与 askCard 同一思路：展示组件只管渲染和点选，解析 / 拼串这些可测的逻辑放这里。
 * 判卷不在前端做——提交后把答案作为一条普通消息发给 agent，由它调
 * check_quiz_answer 对题库标准答案（死数据比对，不是模型拍脑袋）。
 */

export type QuizOptionMap = Record<string, string>;

export type ReadingQuestionView = {
  number: number;
  stem: string;
  options: QuizOptionMap;
};

export type SingleQuiz = {
  kind: "single";
  quizId: string;
  qtype?: string;
  body: string;
};

export type ReadingQuiz = {
  kind: "reading";
  quizId: string;
  qtype?: string;
  passage: string;
  questions: ReadingQuestionView[];
};

export type QuizView = SingleQuiz | ReadingQuiz | null;

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" ? (v as Record<string, unknown>) : null;
}

/** 把 get_quiz 的 tool_result.output 归一化；不是有效题卡（found=false / 缺字段）返回 null。 */
export function extractQuiz(output: unknown): QuizView {
  const o = asRecord(output);
  if (!o || o.found !== true) return null;
  const quizId = typeof o.quiz_id === "string" ? o.quiz_id : "";
  const qtype = typeof o.qtype === "string" ? o.qtype : undefined;

  // 阅读整套卡：文章 + 结构化题目
  const rawQuestions = Array.isArray(o.questions) ? o.questions : null;
  if (rawQuestions && typeof o.passage === "string") {
    const questions: ReadingQuestionView[] = [];
    for (const item of rawQuestions) {
      const q = asRecord(item);
      if (!q) continue;
      const options = asRecord(q.options);
      questions.push({
        number: typeof q.number === "number" ? q.number : questions.length + 1,
        stem: typeof q.stem === "string" ? q.stem : "",
        options: options ? (Object.fromEntries(
          Object.entries(options).filter(([, v]) => typeof v === "string"),
        ) as QuizOptionMap) : {},
      });
    }
    return { kind: "reading", quizId, qtype, passage: o.passage, questions };
  }

  // 单题卡：题面是一整段文本（选项混排在 body 里，前端不拆，只给字母快捷作答）
  if (typeof o.body === "string" && o.body.trim()) {
    return { kind: "single", quizId, qtype, body: o.body };
  }
  return null;
}

const _LETTER = "ABCDEFGHIJKLMNO";

/** 从单题题面里找出实际标出的选项字母（A. / A) / A、 等）；找不出就回退 A-D。 */
export function singleChoiceLetters(body: string): string[] {
  const re = /(?:^|[^\w])([A-G])\s*[.、)．:：]/g;
  const found = new Set<string>();
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    found.add(m[1]);
  }
  const letters = [...found].sort();
  return letters.length >= 2 ? letters : ["A", "B", "C", "D"];
}

/** 阅读作答：{1:"D",2:"A"} → "1.D 2.A"（按题号升序，后端 check_quiz_answer 的串格式）。 */
export function buildReadingAnswers(selections: Record<number, string>): string {
  return Object.keys(selections)
    .map(Number)
    .sort((a, b) => a - b)
    .map((n) => `${n}.${selections[n]}`)
    .join(" ");
}

export { _LETTER };
