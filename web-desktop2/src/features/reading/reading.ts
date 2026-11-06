/**
 * 精读台的领域规则（纯逻辑，不碰网络 / DOM / localStorage）。
 *
 * 设计文档 docs/design/reading-studio-agent-v2-task.md §6：
 * 领域字段、序列化形状和状态规则**只在这里定义一份**，组件不再各写一次。
 * 后端 glyph/reading/* 与这里的字段名一一对应（snake_case），过 HTTP 不改名，
 * 少一层易出错的映射。
 *
 * 三条容易错的口径：
 * - **题目身份 ≠ 显示题号 ≠ 段落号 ≠ 空位号**：question_id 是身份（"q1"），
 *   number 是题卡里的显示题号（46），block_id 是正文块（"p3" / "O"），
 *   blank_no 是七选五的原空号（36）。互相不能替對方位。
 * - **锚点是字符区间 + 稳定块 id**，不是 DOM 序号：同一段里出现两次的同一个词，
 *   选第二次必须标第二次（见 nthIndexOf / occurrenceOf）。
 * - **草稿阶段没有答案**：results 只在 stage === "submitted" 时才可能有内容，
 *   中途读取草稿拿不到任何参考答案或判定。
 */

// --------------------------------------------------------------- 题目与正文结构

/** 三种做题形态（后端 structure.view 给的取值） */
export type QTypeView = "choice" | "matching" | "gap";

export type PassageBlock = {
  id: string; // 稳定身份：p1 / O / blank-36
  kind: "prose" | "blank";
  text: string; // canonical 正文（空白压缩过）
  label: string; // 段落匹配 = 段字母；普通阅读 = 段号；七选五空位 = 原空号
  blank_no: number | null;
  question_id: string | null;
  sentence: string; // 七选五：空位所在原句
};

export type QuestionRef = {
  question_id: string;
  number: number;
  blank_no: number | null;
  block_id: string | null;
};

export type Structure = {
  view: QTypeView | "unsupported";
  blocks: PassageBlock[];
  question_refs: QuestionRef[];
  options: Record<string, string>; // 七选五共享句子 A–G
  picks: string[]; // 段落匹配：实际存在的段字母
  issues: string[];
};

export type PassageQuestion = {
  number: number;
  stem: string;
  options: Record<string, string>;
};

export type PassageMeta = {
  quiz_id: string;
  qtype: string;
  exam: string | null;
  year: string | null;
  source: string | null;
  question_count: number;
};

export type PassageDetail = PassageMeta & {
  passage: string;
  questions: PassageQuestion[];
  structure: Structure;
};

// --------------------------------------------------------------- 一次作答

export type AttemptStage = "draft" | "pending_submission" | "submitted";
export type AttemptMode = "practice" | "exam";

export type TextAnchor = {
  passage_revision: string;
  block_id: string;
  start: number;
  end: number;
  quote: string;
};

export type NoteKind = "word" | "line" | "note";

export type ReadingNote = {
  id: string;
  kind: NoteKind;
  question_id: string | null; // null = 整篇笔记
  anchor: TextAnchor | null; // 自由便签可以没有原句
  body: string;
  created_at: string;
};

export type QuestionState = {
  question_id: string;
  answer: string;
  doubt: boolean;
  candidates: string[]; // 暂定
  excluded: string[]; // 排除
};

export type QuestionResult = {
  question_id: string;
  number: number;
  correct: boolean;
  correct_answer: string;
  your_answer: string | null;
};

export type ModeChange = { from_mode: AttemptMode; to_mode: AttemptMode; at: string };

export type ReadingAttempt = {
  attempt_id: string;
  quiz_id: string;
  stage: AttemptStage;
  qtype: string;
  exam: string | null;
  year: string | null;
  source: string | null;
  passage_revision: string;
  mode: AttemptMode;
  session_id: string;
  started_at: string;
  updated_at: string | null;
  submitted_at: string | null;
  deadline: string | null;
  timed_out: boolean;
  questions: QuestionState[];
  notes: ReadingNote[];
  modes: ModeChange[];
  aids_used: string[];
  results: QuestionResult[];
  submit_no: string;
  source_kind: "live" | "legacy_migrated" | "legacy_summary";
  note_source: string;
};

// --------------------------------------------------------------- 题目 ↔ 结构

/** 这一篇能不能开：不能就显示 issue 里的人话原因，绝不硬渲染一个猜出来的结构。 */
export function structureIssue(structure: Structure | undefined | null): string {
  if (!structure) return "这篇没取到结构，题卡结构需检查";
  if (structure.view === "unsupported") return "这种题型暂时不能在精读台里做";
  return structure.issues[0] ?? "";
}

export function refById(structure: Structure, question_id: string): QuestionRef | undefined {
  return structure.question_refs.find(r => r.question_id === question_id);
}

export function refByNumber(structure: Structure, number: number): QuestionRef | undefined {
  return structure.question_refs.find(r => r.number === number);
}

/** 显示题号 → 题目身份（没有登记就猜 `q<number>`，但注册过优先用它） */
export function questionIdOfNumber(structure: Structure, number: number): string {
  return refByNumber(structure, number)?.question_id ?? `q${number}`;
}

export function blockOfQuestion(structure: Structure, question_id: string): PassageBlock | undefined {
  const ref = refById(structure, question_id);
  if (ref?.block_id) return structure.blocks.find(b => b.id === ref.block_id);
  // 段落匹配 / 七选五：作答本身就是某个块的身份（"O" / "blank-36"）
  return undefined;
}

/** 七选五：全部空位，按显示题号排（顺序跟着题卡走，不跟着后端给的块顺序） */
export function gapBlanks(structure: Structure): PassageBlock[] {
  const blanks = structure.blocks.filter(b => b.kind === "blank");
  const order = (id: string | null) => {
    if (!id) return Number.MAX_SAFE_INTEGER;
    return refById(structure, id)?.number ?? Number.MAX_SAFE_INTEGER;
  };
  return [...blanks].sort((a, b) => order(a.question_id) - order(b.question_id));
}

// --------------------------------------------------------------- 作答状态（按题）

export function emptyState(question_id: string): QuestionState {
  return { question_id, answer: "", doubt: false, candidates: [], excluded: [] };
}

export function stateOf(attempt: ReadingAttempt, question_id: string): QuestionState {
  return attempt.questions.find(q => q.question_id === question_id) ?? emptyState(question_id);
}

export function isAnswered(state: QuestionState): boolean {
  return state.answer.trim() !== "";
}

/** 写入一道题的答案（不可变）。同identity 覆盖，别的题原样保留。 */
export function withAnswer(
  states: QuestionState[],
  question_id: string,
  answer: string,
): QuestionState[] {
  const prev = states.find(q => q.question_id === question_id) ?? emptyState(question_id);
  const next: QuestionState = { ...prev, answer: answer.trim().toUpperCase() };
  return [...states.filter(q => q.question_id !== question_id), next];
}

/** 清空本题：整道题回到未答（答案、暂定、排除、存疑一起清，不留半个状态） */
export function clearQuestion(states: QuestionState[], question_id: string): QuestionState[] {
  const prev = states.find(q => q.question_id === question_id);
  if (!prev) return states;
  if (!prev.answer && !prev.doubt && !prev.candidates.length && !prev.excluded.length) {
    return states.filter(q => q.question_id !== question_id);
  }
  return states.filter(q => q.question_id !== question_id);
}

export function withDoubt(
  states: QuestionState[],
  question_id: string,
  doubt: boolean,
): QuestionState[] {
  const prev = states.find(q => q.question_id === question_id) ?? emptyState(question_id);
  return [...states.filter(q => q.question_id !== question_id), { ...prev, doubt }];
}

export function toggleDoubt(states: QuestionState[], question_id: string): QuestionState[] {
  const prev = states.find(q => q.question_id === question_id) ?? emptyState(question_id);
  return withDoubt(states, question_id, !prev.doubt);
}

/** 候选 / 排除：只动**当前题**的那一份（第 36 题排除 O 不影响第 39 题选 O） */
export function toggleInState(
  states: QuestionState[],
  question_id: string,
  field: "candidates" | "excluded",
  value: string,
): QuestionState[] {
  const prev = states.find(q => q.question_id === question_id) ?? emptyState(question_id);
  const list = prev[field];
  const nextList = list.includes(value) ? list.filter(x => x !== value) : [...list, value];
  const next: QuestionState = { ...prev, [field]: nextList };
  return [...states.filter(q => q.question_id !== question_id), next];
}

/** 已排除的段重新被明确选入：清掉它在这题上的冲突标记 */
export function answerKeepsExcludedOff(
  state: QuestionState,
  answer: string,
): QuestionState {
  if (!state.excluded.includes(answer)) return state;
  return { ...state, excluded: state.excluded.filter(x => x !== answer) };
}

/**
 * 七选五：一个共享句子只能用在一个空里。
 * 返回接下来要改的两道题（旧空清空 + 新空填上）；本来就没被用过则只改新空。
 */
export function moveGapOption(
  states: QuestionState[],
  question_id: string,
  letter: string,
): { states: QuestionState[]; movedFrom: QuestionState | null } {
  const holder = states.find(q => q.question_id !== question_id && q.answer === letter);
  let next = states.filter(q => q.question_id !== holder?.question_id);
  if (holder) {
    next = [
      ...next,
      { ...holder, answer: "" },
    ];
  }
  const prev = states.find(q => q.question_id === question_id) ?? emptyState(question_id);
  next = [
    ...next.filter(q => q.question_id !== question_id),
    { ...prev, answer: letter },
  ];
  return { states: next, movedFrom: holder ?? null };
}

export function optionUsedBy(states: QuestionState[], letter: string): QuestionState | undefined {
  return states.find(q => q.answer === letter);
}

// --------------------------------------------------------------- 笔记与锚点

export function notesOfQuestion(notes: ReadingNote[], question_id: string): ReadingNote[] {
  // 整篇笔记（question_id = null）不会随机关联到一道题
  return notes.filter(n => n.question_id === question_id);
}

export function wholeNotes(notes: ReadingNote[]): ReadingNote[] {
  return notes.filter(n => n.question_id === null);
}

/** 第 n 次出现的下标（0 起）；找不到返回 -1。第二次选同一个词就靠它。 */
export function nthIndexOf(text: string, quote: string, occurrence: number): number {
  let from = 0;
  for (let i = 0; i <= occurrence; i++) {
    const idx = text.indexOf(quote, from);
    if (idx < 0) return -1;
    if (i === occurrence) return idx;
    from = idx + 1;
  }
  return -1;
}

/** 选一段文字 → 锚点。跨块的选区不支持（宁可不记，也不记成一个错位区间）。 */
export function makeAnchor(args: {
  blockId: string;
  blockText: string;
  quote: string;
  occurrence?: number;
  revision?: string;
}): TextAnchor | null {
  const occurrence = args.occurrence ?? 0;
  const start = nthIndexOf(args.blockText, args.quote, occurrence);
  if (start < 0) return null;
  return {
    passage_revision: args.revision ?? "",
    block_id: args.blockId,
    start,
    end: start + args.quote.length,
    quote: args.quote,
  };
}

/**
 * 锚点能不能信：**必须和这一版的正文对得上**（quote 与切片一致）。
 * 对不上就标"待核对"，绝不挪到相似句子上去高亮。
 */
export function anchorStatus(
  anchor: TextAnchor | null,
  blockText: string | undefined,
  revision: string,
): "ok" | "stale" | "mismatch" {
  if (!anchor) return "ok";
  if (anchor.passage_revision && anchor.passage_revision !== revision) return "stale";
  if (blockText === undefined) return "mismatch";
  const slice = blockText.slice(anchor.start, anchor.end);
  return slice === anchor.quote ? "ok" : "mismatch";
}

export type RenderMark = { start: number; end: number; kind: "word" | "line" | "note" | "evidence" };
export type RenderSegment = { t: string; kind?: RenderMark["kind"] };

/**
 * 一段正文 → 高亮片段。按**偏移量**切、不是按文本搜索 happen 的：
 * 同一个词出现两次时，两次各自的标记各归各。
 * 重叠区间取优先级：用户自己的标记 > Agent 证据。
 */
export function renderSegments(text: string, marks: RenderMark[]): RenderSegment[] {
  const rank: Record<RenderMark["kind"], number> = {
    line: 3,
    note: 3,
    word: 2,
    evidence: 1,
  };
  const clean = marks
    .filter(m => m.end > m.start && m.start >= 0 && m.end <= text.length)
    .map(m => ({ ...m, r: rank[m.kind] }));
  const edges = Array.from(
    new Set([0, text.length, ...clean.flatMap(m => [m.start, m.end])]),
  ).sort((a, b) => a - b);

  const out: RenderSegment[] = [];
  for (let i = 0; i < edges.length - 1; i++) {
    const start = edges[i];
    const end = edges[i + 1];
    if (end <= start) continue;
    const covering = clean.filter(m => m.start <= start && m.end >= end);
    if (!covering.length) {
      out.push({ t: text.slice(start, end) });
      continue;
    }
    const best = covering.reduce((acc, m) => (m.r > acc.r ? m : acc));
    out.push({ t: text.slice(start, end), kind: best.kind });
  }
  return out;
}

// --------------------------------------------------------------- 复盘优先级

export type ReviewItem = {
  question_id: string;
  number: number;
  stem: string;
  options: Record<string, string>;
  result: QuestionResult | null;
  state: QuestionState;
  noteCount: number;
};

export function resultOf(attempt: ReadingAttempt, question_id: string): QuestionResult | null {
  return attempt.results.find(r => r.question_id === question_id) ?? null;
}

export function missed(result: QuestionResult | null): boolean {
  return !result || result.your_answer === null;
}

/**
 * 这一题值不值得回看：错题 / 漏题 / 答对但存疑 / 有主动笔记。
 * 小的排在前面（错题最先），顺序对应设计文档 §5.4 的优先列表。
 *
 * **所有题都能请求讲解**，这里只决定优先列表（开屏默认先摊开哪些）。
 */
export function reviewPriority(item: ReviewItem): number {
  const r = item.result;
  if (!r) return 5; // 还没判卷（正常流程不该出现，兜底当最值得看的处理）
  if (!r.correct) return missed(r) ? 1 : 0; // 漏答紧跟着错题
  if (item.state.doubt) return 2; // 答对但存疑：很容易被自己忽略
  if (item.noteCount > 0) return 3; // 带着主动疑问
  return 4; // 普通答对
}

export function needsReview(item: ReviewItem): boolean {
  return reviewPriority(item) <= 3;
}

export type ReviewRow = ReviewItem & {
  notes: ReadingNote[];
  missed: boolean;
  needsReview: boolean;
};

/**
 * 复盘列表。「就是 Root 身份 + 显示题号 + 题干 + 这次的实际作答 + 你当时写的笔记」，
 * 一次拼齐，组件不再到处 papel 抓资料。
 */
export function buildReviewItems(detail: PassageDetail, attempt: ReadingAttempt): ReviewRow[] {
  return detail.structure.question_refs
    .map(ref => {
      const question = detail.questions.find(q => q.number === ref.number);
      const result = resultOf(attempt, ref.question_id);
      const state = stateOf(attempt, ref.question_id);
      const notes = notesOfQuestion(attempt.notes, ref.question_id);
      const base: ReviewItem = {
        question_id: ref.question_id,
        number: ref.number,
        stem: question?.stem ?? "",
        options: question?.options ?? {},
        result,
        state,
        noteCount: notes.length,
      };
      return { ...base, notes, missed: missed(result), needsReview: needsReview(base) };
    })
    .sort((a, b) => a.number - b.number);
}

/** 这次作答用了多久（从开始做到交卷）。 */
export function elapsedLabel(attempt: ReadingAttempt): string {
  const start = Date.parse(attempt.started_at);
  const end = attempt.submitted_at ? Date.parse(attempt.submitted_at) : NaN;
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  const secs = Math.max(0, Math.round((end - start) / 1000));
  return `用时 ${formatClock(secs * 1000)}`;
}

/** 三种做题形态的显示名（四级考纲里「仔细阅读 / 长篇阅读」的说法，界面沿用） */
export const VIEW_TITLE: Record<string, string> = {
  choice: "仔细阅读",
  matching: "长篇阅读（段落匹配）",
  gap: "七选五",
};

/** 「让 Glyph 讲这题」按钮的文案：答对且存疑时突出为「确认我的思路」 */
export function explainLabel(item: ReviewItem): string {
  const r = item.result;
  if (r?.correct && item.state.doubt) return "确认我的思路";
  return "让 Glyph 讲这题";
}

// --------------------------------------------------------------- 模式 / 计时

/** 语言辅助只在学习模式下可用；模拟模式一字辅助都不给。 */
export function aidAllowed(attempt: ReadingAttempt): boolean {
  return attempt.mode === "practice";
}

/** 辅助种类的中文名：这个记录是给用户和 Agent 看的，别写 LOUIS 式的代号。 */
export function aidLabelFor(kind: string): string {
  const labels: Record<string, string> = {
    lookup: "查词",
    translate: "翻译这句",
    parse: "拆句",
    speak: "朗读",
  };
  return labels[kind] ?? kind;
}

/** 绝对 deadline：刷新 / 切后台后按当前时间算，不靠定时器走过多少次。 */
export function remainingMs(deadline: string | null | undefined, now: number): number | null {
  if (!deadline) return null;
  const target = Date.parse(deadline);
  if (Number.isNaN(target)) return null;
  return Math.max(0, target - now);
}

export function isExpired(deadline: string | null | undefined, now: number): boolean {
  const left = remainingMs(deadline, now);
  return left !== null && left <= 0;
}

export function formatClock(ms: number): string {
  const total = Math.ceil(ms / 1000);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

/** 漏答题号：手动交卷前提醒用（给具体题号，不给模糊的"还有几题"） */
export function missingNumbers(structure: Structure, attempt: ReadingAttempt): number[] {
  return structure.question_refs
    .filter(r => !isAnswered(stateOf(attempt, r.question_id)))
    .map(r => r.number)
    .sort((a, b) => a - b);
}

// --------------------------------------------------------------- 判卷 → 复盘视图

export function mapJudgeToReview(
  results: QuestionResult[],
  questions: PassageQuestion[],
): Array<QuestionResult & { stem: string; missed: boolean; options: Record<string, string> }> {
  const byNumber = new Map(questions.map(q => [q.number, q]));
  return [...results]
    .sort((a, b) => a.number - b.number)
    .map(r => ({
      ...r,
      stem: byNumber.get(r.number)?.stem ?? "",
      options: byNumber.get(r.number)?.options ?? {},
      missed: r.your_answer === null,
    }));
}

// --------------------------------------------------------------- 讲题提示词（Phase A）

/**
 * 「让 Glyph 讲这题」用的上下文。
 *
 * 这里刻意**拼全**这次作答的事实：哪一篇（quiz_id）、哪一次（attempt_id）、
 * 哪一题（question_id + 显示题号 + 空位号）、我选了什么、我当时标了什么疑、
 * 我写了哪些笔记（含原句）、这题在原文的哪一块。以前缺的就是这些——只带题干，
 * 模型只能凭这篇曾经 достып answer 说话。
 */
export function buildExplainPrompt(args: {
  attempt: ReadingAttempt;
  detail: PassageDetail;
  question_id: string;
}): string {
  const { attempt, detail, question_id } = args;
  const ref = refById(detail.structure, question_id);
  const number = ref?.number ?? 0;
  const question = detail.questions.find(q => q.number === number);
  const state = stateOf(attempt, question_id);
  const result = resultOf(attempt, question_id);
  const notes = notesOfQuestion(attempt.notes, question_id);
  const block = ref?.block_id ? detail.structure.blocks.find(b => b.id === ref.block_id) : undefined;

  const opts = Object.entries(question?.options ?? {})
    .map(([k, v]) => `${k}. ${v}`)
    .join("\n");
  const noteLines = notes
    .map(n => {
      const quote = n.anchor ? `（原句：“${n.anchor.quote}”）` : "";
      return `- [${n.kind}] ${n.body}${quote}`;
    })
    .join("\n");

  const lines = [
    `我在精读台做了一篇「${detail.qtype}」（${[detail.exam, detail.year, detail.source].filter(Boolean).join(" · ")}）。`,
    `篇章 quiz_id=${detail.quiz_id}；这次作答 attempt_id=${attempt.attempt_id}。`,
    "",
    `请讲第 ${number} 题${ref?.blank_no ? `（原题空号 ${ref.blank_no}）` : ""}，question_id=${question_id}：`,
    `【题干】${question?.stem ?? ""}`,
    opts ? `【选项】\n${opts}` : "",
    result ? `【标准答案】${result.correct_answer}` : "",
    state.answer ? `【我的作答】${state.answer}（${result?.correct ? "答对" : result ? "答错" : "未判卷"}）` : "【我的作答】这题我漏答了。",
    state.doubt ? "【我自己标了存疑】" : "",
    state.excluded.length ? `【我这题排除过】${state.excluded.join("、")}` : "",
    state.candidates.length ? `【我这题暂定过】${state.candidates.join("、")}` : "",
    noteLines ? `【我做题时写的依据 / 疑问】\n${noteLines}` : "",
    block?.text ? `【这题相关的原文块】${block.text}` : "",
    "",
    "请讲清楚：这句原文为什么能推出答案、我选的（或漏掉的）为什么不成立。",
    "不知道就说不知道，不要为了圆答案编理由。",
  ].filter(Boolean);
  return lines.join("\n");
}
