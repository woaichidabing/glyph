/* ============================================================
   全局数据契约（方案第六节）
   这里只放"形状"，不放实现；后端字段变动只改这一处。
   ============================================================ */

export type Accent = "uk" | "us";

export type Example = { en: string; zh: string };

export type Sense = {
  pos: string; // 词性：n. / v. / adj. …
  translation: string; // 中文释义
  examples: Example[];
};

/** 词条的来源，界面上要诚实标注，别让用户以为查到了真词库 */
export type DictSource = "cache" | "api" | "mini";

export type DictEntry = {
  word: string;
  phonetic: { uk: string; us: string };
  audio?: { uk?: string; us?: string };
  senses: Sense[];
  forms?: string[];
  /** 英文释义（后端词库直接给的整句释义；旧缓存可能没有） */
  definitions?: string[];
  synonyms?: string[];
  antonyms?: string[];
  tags?: string[];
  /** 用户查的是变形（went）、词条反查到原形时：回显用户实际输入的变形 */
  queried_form?: string;
  /** 词本身是另一词的变形（children→child）时：其原形 */
  base_form?: string;
  source: DictSource;
};

/** 生词本条目：收藏时把首条释义快照下来，离线也能看 */
export type WordbookItem = {
  word: string;
  addedAt: number;
  phonetic?: { uk: string; us: string };
  gloss?: string;
};

export type HistoryItem = {
  word: string;
  at: number;
  /** 同一个词累计查过几次（后端给；旧 localStorage 数据没有这个字段） */
  count?: number;
};

/* ---------- 计划与计时 ---------- */
export type Task = {
  id: string;
  title: string;
  date: string; // YYYY-MM-DD
  start?: string; // HH:mm
  durationMin: number;
  tag?: string;
  note?: string;
  done: boolean;
  doneAt?: number;
  /** 实际用时（分钟），完成后由用户核对或计时器写入；统计以此为据 */
  actualMin?: number;
};

export type TimerSession = {
  id: string;
  taskId?: string;
  startedAt: number; // 时间戳，不用累加，避免后台节流漂移
  endedAt?: number;
  seconds: number;
  mode: "countup" | "countdown";
};

/* ---------- 词书与背单词（方案 5.6） ---------- */
export type Wordbook = {
  id: string;
  name: string;
  origin: "preset" | "custom" | "errors" | "history";
  /** 分组：升学 / 大学 / 出国 / 场景 / 专项 */
  group: string;
  total: number;
  /** 难度 1–5 */
  difficulty: number;
  cover: string;
  tags?: string[];
  learners?: number;
};

export type WordCardState = {
  word: string;
  bookId: string;
  status: "new" | "learning" | "review" | "done";
  /** SM-2 简易因子（旧卡遗留，新算法不再用它，只用于迁入 FSRS 时估难度） */
  ease: number;
  intervalDays: number;
  dueDate: string; // YYYY-MM-DD
  reps: number;
  lapses: number; // 错几次；≥2 才值得单独出卡

  /* ---- FSRS 三量（见 adapters/srs.ts 顶部的说明）---- */
  /** 稳定度（天）：S=10 表示"过 10 天还记得约九成" */
  s?: number;
  /** 难度 1–10，越大越难 */
  d?: number;
  /** 上一次复习时被提取到的概率（0–1），界面上要能说"来的时候还记得多少" */
  lastR?: number;
  /** 上一次复习是哪天（YYYY-MM-DD），算 R 要用 */
  lastReviewAt?: string;
  /* ---- 作答统计（选择题式学习用；自动定档要靠它，也用来展示"正确率"）---- */
  seen?: number;
  right?: number;
  /** 上一次作答用时（毫秒） */
  lastMs?: number;
  /**
   * 连对次数：满 3 才算学会，中途错一次清零。
   * 为什么存在卡片上而不是只留在内存里：背到一半关掉页面，回来不该从头再来。
   * 配 streakAt 一起看：连对是"当天"的事，隔天重来一次不算偷懒——
   * 昨天连对三次的词，今天再见面时依然要从零数起。
   */
  streak?: number;
  /** 连对次数是哪一天记的（YYYY-MM-DD） */
  streakAt?: string;
  /** 直接标成熟词：本次不再出现，按算法排到以后复习 */
  known?: boolean;
};

export type DailyQueue = {
  date: string;
  newWords: string[];
  reviewWords: string[];
  wrongWords: string[];
  doneNew: number;
  doneReview: number;
  doneWrong: number;
  /** 随心背：这份队列是从第几个词开始的。游标一动，队列就得重排 */
  freeFrom?: number;
};

export type ErrorWord = {
  word: string;
  from?: string; // 来源：哪篇作文 / 哪一次查词
  sentence?: string;
  reason?: string;
  addedAt: number;
  /** 连续答对次数，满 3 次自动移出 */
  goodStreak: number;
  lapses: number;
};

/* ---------- 卡片库（方案 5.8 / 第二十四节） ----------
   两种卡各有各的版式，但共用同一套纸面外框与同一条导出链路。
   用可辨别联合而不是一堆可选字段：渲染时 TypeScript 才知道该读哪些字段。 */

export type CardBase = {
  id: string;
  createdAt: number;
  /** 从哪儿来的：哪本词书 / 哪一次问答 */
  source?: string;
};

/** 单词卡：一错一卡。错够两次才值得单独立一张 */
export type WordCardItem = CardBase & {
  kind: "word";
  word: string;
  phonetic?: string;
  pos?: string;
  translation: string;
  example?: { en: string; zh: string };
  lapses: number;
  book?: string;
};

/** 错题卡：一题一卡。来自拍照问答或作文批改 */
export type QuizCardItem = CardBase & {
  kind: "quiz";
  title: string;
  question: string;
  answer: string;
  points?: string[];
  /** 对照表：易混的两项并排，比写在正文里好记 */
  compare?: { head: string[]; rows: string[][] };
  mnemonic?: string;
  tag?: string;
};

/** 作文卡：一处批改一张，来自作文批改 */
export type EssayCardItem = CardBase & {
  kind: "essay";
  mark: MarkKind;
  sentence: string;
  fix?: string;
  why?: string;
  /** 出自哪一篇作文 */
  essay?: string;
};

export type SavedCard = WordCardItem | QuizCardItem | EssayCardItem;

/* ---------- 批改 ---------- */
export type MarkKind = "error" | "suggest" | "highlight" | "good" | "structure" | "chinglish";

export type Issue = {
  kind: MarkKind;
  span?: [number, number];
  text: string;
  fix?: string;
  why?: string;
};

export type EssayReport = {
  score: number;
  dimensions: { name: string; score: number; note?: string }[];
  issues: Issue[];
  rewrite?: string;
};
