/**
 * 收卷子（题库导入）：一次能收多套。
 *
 * - 走文件：POST /api/quiz/import/batch（一次最多 BATCH_MAX_PAPERS 个文件，
 *   后端按文件名自动配「卷子 + 答案」，一套坏了不牵连其余）。
 * - 走贴字：POST /api/quiz/import（老的单套接口，答案要对得上才行）。
 *
 * 拆题在后端本地完成（不联网、不调模型）；答案里找不到的题会被后端质检闸丢弃，宁缺毋滥。
 * 写作没有标准答案：只收题目要求，判卷走批改，不进 check_quiz_answer。
 */

/**
 * 一次收进去的题型（后端默认「全部」，逐个跑 importer）。
 * 界面不再让用户单选——一套卷子本来就含这几块，点四次太别扭。
 * 文案与后端 importer 分派表严格一致。
 */
export const INTAKE_QTYPES = [
  { value: "仔细阅读", hint: "Section C · 一篇长文章配单选，一篇拆一套卡" },
  { value: "段落匹配", hint: "Section B · 段落 A–M，把陈述句对回段落" },
  { value: "选词填空", hint: "Section A · 词库 15 词，选词填 10 个空" },
  { value: "写作", hint: "Part I · 只收题目要求，作文没有标准答案、靠批改" },
] as const;

/** 高考卷面是另一套拆法（后端按考试登记题型组，这里文案要跟着换） */
export const GAOKAO_QTYPES = [
  { value: "阅读理解", hint: "A/B/C/D 四篇，每篇三到四道单选，一篇拆一套卡" },
  { value: "七选五", hint: "从 A–G 里挑五个填进空白处，有两项多余" },
  { value: "完形填空", hint: "一篇短文 15 个空，每空四选一" },
  { value: "语法填空", hint: "10 个空填单词（给词变形或填虚词），答案是词不是字母" },
  { value: "书面表达", hint: "应用文 + 读后续写，没有标准答案、靠批改" },
] as const;

/** 考试 → 会拆哪几块。认不出的考试走四级那组（与后端 qtypes_for 同口径）。 */
export function qtypesForExam(exam: string): readonly { value: string; hint: string }[] {
  return exam.trim() === "高考" ? GAOKAO_QTYPES : INTAKE_QTYPES;
}
export type IntakeQtype = (typeof INTAKE_QTYPES)[number]["value"];

/** 考试快选词（输入框仍可自由填，如「专八」「雅思」） */
export const INTAKE_EXAMS = ["四级", "六级", "考研", "高考"] as const;

/** 文件选择器的 accept 串；老 .doc 后端不认，UI 先拦下并提示另存 */
export const INTAKE_ACCEPT = ".pdf,.docx,.txt,.md,.text";

/** 与后端 extract_text 的分派表对齐；无后缀按纯文本处理（和后端 "" 分支一致） */
const SUPPORTED_EXT = [".pdf", ".docx", ".txt", ".md", ".text"];

/** 一次批量最多选几个文件：拆题是本机 CPU 活，一次太多会卡住界面 */
export const BATCH_MAX_PAPERS = 3;

export type IntakeSide = { file: File | null; text: string };

export type IntakeForm = {
  exam: string;
  year: string;
  source: string;
  questions: IntakeSide;
  answers: IntakeSide;
};

/** 文件类型前端先拦一道（拖拽绕不过 accept 过滤器），口径与后端一致 */
export function fileSupported(name: string): boolean {
  const at = name.lastIndexOf(".");
  if (at < 0) return true; // 无后缀 → 后端按纯文本读
  return SUPPORTED_EXT.includes(name.slice(at).toLowerCase());
}

/** 文件大小说人话（已选文件卡右上角小字用） */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// ------------------------------------------------- 文件名识别 & 批量精确配对

/** 考试名 → 落盘 key 前缀：四六级对齐出厂卡 cet4/cet6，其它保留中文原名 */
const EXAM_CODE_MAP: Record<string, string> = { 四级: "cet4", 六级: "cet6" };

export function examCode(exam: string): string {
  const t = exam.trim();
  if (EXAM_CODE_MAP[t]) return EXAM_CODE_MAP[t];
  // 去掉不能当文件名的字符（Windows 保留符 + 空白），中文照留（后端安全名允许中文）
  return t.replace(/[\s/\\:*?"<>|]+/g, "-");
}

/** 一套的落盘 key：cet4-2026-set1；没填年份也不留空段 */
export function batchKey(exam: string, year: string, setNo: number): string {
  return [examCode(exam), year.trim() || null, `set${setNo}`].filter(Boolean).join("-");
}

function extOf(name: string): string {
  const at = name.lastIndexOf(".");
  return at > 0 ? name.slice(at).toLowerCase() : "";
}

/** 批量上传时给文件加 q_ / a_ 前缀，让后端 pair_files 不靠猜、精确配成一套 */
export function batchFileName(originalName: string, role: "q" | "a", key: string): string {
  return `${role}_${key}${extOf(originalName)}`;
}

const _CN_NUM: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6 };

export type GuessedPaperMeta = { exam?: string; year?: string; setNo?: number };

/**
 * 从文件名智能认出考试 / 年份 / 套次，用来在选好文件后自动填芯片。
 * 只认有把握的：年份只认四位 20xx（避免把题号「26」当年份），认不出的字段就不给。
 */
export function guessPaperMeta(filename: string): GuessedPaperMeta {
  const lower = filename.toLowerCase();
  const meta: GuessedPaperMeta = {};

  if (/四级|cet[-_ ]?4/.test(lower)) meta.exam = "四级";
  else if (/六级|cet[-_ ]?6/.test(lower)) meta.exam = "六级";
  else if (/考研|研究生/.test(filename)) meta.exam = "考研";
  else if (/高考|新课标|全国[甲乙丙]?卷/.test(filename)) meta.exam = "高考";

  const ym = /(20\d{2})/.exec(filename);
  if (ym) meta.year = ym[1];

  const cn = /第\s*([0-9一二三四五六])\s*套/.exec(filename);
  if (cn) meta.setNo = /[0-9]/.test(cn[1]) ? Number(cn[1]) : _CN_NUM[cn[1]];
  else {
    const en = /set[-\s]?(\d)/.exec(lower);
    if (en) meta.setNo = Number(en[1]);
  }

  return meta;
}

/** 批量导入里的一个套卡槽：卷子必填，答案单独给就放 answerFile，答案在卷子里就给 null */
export type BatchSlot = {
  questionFile: File;
  answerFile: File | null;
  /** 一般不填，由考试年份 + 槽序号生成；特殊命名才显式给 */
  key?: string;
};

// --------------------------------- 批量文件按套配对（与后端 glyph.quizzes.batch.pair_files 同口径，改规则两边一起改）
const _Q_PREFIX_RE = /^[qQ]_/;
const _A_PREFIX_RE = /^[aA]_/;
// 「答案与详解」要排在「答案」前，先整块吃掉，免得只删了「答案」、留下「与详解」
const _ANSWER_MARK_G_RE = /(答案与详解|答案|answer|ans\b|key\b)/gi;
const _ANSWER_MARK_I_RE = /(答案与详解|答案|answer|ans\b|key\b)/i;
const _EMPTY_BRACKET_RE = /[（(]\s*[）)]/g;
const _EDGE_TRIM_RE = /^[\s_\-.（）()]+|[\s_\-.（）()]+$/g;

function stemOfName(name: string): string {
  const at = name.lastIndexOf(".");
  return at > 0 ? name.slice(0, at) : name;
}

/** 剥掉 q_/a_ 前缀与答案标记，得到这套卷子的本名（对齐后端 _strip_marks） */
export function stripPaperMarks(stem: string): string {
  const s = stem
    .replace(_A_PREFIX_RE, "")
    .replace(_Q_PREFIX_RE, "")
    .replace(_ANSWER_MARK_G_RE, "")
    .replace(_EMPTY_BRACKET_RE, "");
  return s.replace(_EDGE_TRIM_RE, "");
}

/** 文件名（看不含扩展名的主干）是否答案侧：a_ 前缀，或带「答案 / answer / key」标记 */
function isAnswerStem(stem: string): boolean {
  return _A_PREFIX_RE.test(stem) || _ANSWER_MARK_I_RE.test(stem);
}

export type PairIntoSlotsResult = {
  /** 配成的套（都有卷子文件），按卷子文件出现顺序排，最多 maxPapers 套 */
  slots: BatchSlot[];
  /** 判成答案、却没配上同名卷子的文件（单独提示，别静默丢） */
  orphanAnswers: File[];
  /** 超出 maxPapers、这批没收下的卷子文件 */
  droppedPapers: File[];
};

/**
 * 把一次选入的多个文件按命名配成「一套 = 卷子 + 可选答案」，规则与后端 pair_files 一致。
 * 关键：上限按**套**数算，不是文件数——3 套若题、答分开放，最多是 6 个文件，
 * 不能像旧逻辑那样数到第 3 个文件就把答案册和后面的卷子全丢掉。
 */
export function pairIntoSlots(
  files: File[],
  maxPapers: number = BATCH_MAX_PAPERS,
): PairIntoSlotsResult {
  const questions: { key: string; file: File }[] = [];
  const orphans: { key: string; file: File }[] = [];
  for (const file of files) {
    const stem = stemOfName(file.name);
    const key = stripPaperMarks(stem) || stem;
    if (isAnswerStem(stem)) orphans.push({ key, file });
    else questions.push({ key, file });
  }

  const questionByKey = new Map<string, File>();
  for (const q of questions) if (!questionByKey.has(q.key)) questionByKey.set(q.key, q.file);

  const usedKeys = new Set<string>();
  const answerByKey = new Map<string, File>();
  const orphanAnswers: File[] = [];
  for (const a of orphans) {
    if (questionByKey.has(a.key) && !usedKeys.has(a.key)) {
      answerByKey.set(a.key, a.file);
      usedKeys.add(a.key);
    } else {
      orphanAnswers.push(a.file);
    }
  }

  const all: BatchSlot[] = questions.map(q => ({
    questionFile: q.file,
    answerFile: answerByKey.get(q.key) ?? null,
  }));
  return {
    slots: all.slice(0, maxPapers),
    droppedPapers: all.slice(maxPapers).map(s => s.questionFile),
    orphanAnswers,
  };
}

/**
 * 把逐套槽位组装成批量表单：每套卷子改名 q_{key}、单独答案改名 a_{key}，
 * 答案在同一份的只传 q（不造空答案）。后端按前缀精确配对，不依赖原文件名是否规范。
 */
export function buildBatchForm(
  meta: { exam: string; year: string; source?: string },
  slots: BatchSlot[],
): FormData {
  const fd = new FormData();
  fd.append("exam", meta.exam.trim());
  if (meta.year.trim()) fd.append("year", meta.year.trim());
  if (meta.source?.trim()) fd.append("source", meta.source.trim());

  slots.forEach((slot, i) => {
    const key = slot.key ?? batchKey(meta.exam, meta.year, i + 1);
    fd.append("files", slot.questionFile, batchFileName(slot.questionFile.name, "q", key));
    if (slot.answerFile) {
      fd.append("files", slot.answerFile, batchFileName(slot.answerFile.name, "a", key));
    }
  });
  return fd;
}

export type ImportOutcome =
  | {
      ok: true;
      count: number;
      imported: string[];
      /** 每个题型各拆出哪些卡，界面按题型报数用 */
      byQtype: Record<string, string[]>;
      /** 尝试了但没拆出题的题型（卷子里没有这块），不算失败 */
      missing: string[];
      message: string | null;
    }
  | { ok: false; error: string };

/** 收进题库。失败时优先带回后端的 detail（后端 detail 已是人话），fetch 挂了给兜底文案。 */
export async function importPaper(form: IntakeForm): Promise<ImportOutcome> {
  const fd = new FormData();
  fd.append("exam", form.exam.trim());
  if (form.year.trim()) fd.append("year", form.year.trim());
  if (form.source.trim()) fd.append("source", form.source.trim());
  // 不传 qtype → 后端默认「全部」：一次把三个客观题型都拆了
  // 文件优先：有文件只传文件；没文件才传贴字（与后端 `_upload_text(...) or ..._text` 同序）
  const { questions, answers } = form;
  if (questions.file) fd.append("questions_file", questions.file, questions.file.name);
  else if (questions.text.trim()) fd.append("questions_text", questions.text);
  if (answers.file) fd.append("answers_file", answers.file, answers.file.name);
  else if (answers.text.trim()) fd.append("answers_text", answers.text);

  try {
    const res = await fetch("/api/quiz/import", { method: "POST", body: fd });
    if (!res.ok) return { ok: false, error: await extractDetail(res) };
    const data = (await res.json()) as {
      imported?: string[];
      count?: number;
      by_qtype?: Record<string, string[]>;
      missing?: string[];
      message?: string;
    };
    return {
      ok: true,
      count: data.count ?? 0,
      imported: data.imported ?? [],
      byQtype: data.by_qtype ?? {},
      missing: data.missing ?? [],
      message: data.message ?? null,
    };
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

/** 批量里每一套的结果（后端 papers[] 的原样） */
export type PaperOutcome = {
  key: string;
  count: number;
  byQtype: Record<string, string[]>;
  missing: string[];
  error: string | null;
};

/**
 * 把后端一套的结果归一成界面能直接渲染的形状。后端给的是 snake_case（by_qtype），
 * 失败套还只有 key + error（不带 count / missing）。不在这兜底，结果页一读
 * byQtype / missing.length 就是 undefined，直接把整页带白。
 */
export function normalizePaperOutcome(raw: unknown): PaperOutcome {
  const p = (raw ?? {}) as Record<string, unknown>;
  const byQtype =
    (p.byQtype as Record<string, string[]> | undefined) ??
    (p.by_qtype as Record<string, string[]> | undefined) ??
    {};
  return {
    key: typeof p.key === "string" ? p.key : "",
    count: typeof p.count === "number" ? p.count : 0,
    byQtype,
    missing: Array.isArray(p.missing) ? (p.missing as string[]) : [],
    error: typeof p.error === "string" ? p.error : null,
  };
}

export type BatchOutcome =
  | {
      ok: true;
      count: number;
      imported: string[];
      papers: PaperOutcome[];
      /** 单个文件读不了之类的问题（不炸整批，其余照收） */
      problems: string[];
      message: string | null;
    }
  | { ok: false; error: string };

/**
 * 提交一份已经组好的批量表单（逐套槽位用 buildBatchForm 生成、文件已按 q_/a_ 改名）。
 * 配对在前端就定死了，不靠后端猜原文件名。
 */
export async function submitBatchForm(fd: FormData): Promise<BatchOutcome> {
  try {
    const res = await fetch("/api/quiz/import/batch", { method: "POST", body: fd });
    if (!res.ok) return { ok: false, error: await extractDetail(res) };
    const data = (await res.json()) as {
      count?: number;
      imported?: string[];
      papers?: Array<Record<string, unknown>>;
      problems?: string[];
      message?: string;
    };
    return {
      ok: true,
      count: data.count ?? 0,
      imported: data.imported ?? [],
      papers: (Array.isArray(data.papers) ? data.papers : []).map(normalizePaperOutcome),
      problems: data.problems ?? [],
      message: data.message ?? null,
    };
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

async function extractDetail(res: Response): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown };
    if (typeof data.detail === "string" && data.detail) return data.detail;
  } catch { /* 响应不是 JSON，走兜底 */ }
  return "没收进去——请检查文件后重试。";
}
