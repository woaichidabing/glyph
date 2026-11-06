/**
 * 精读台 IO： passages（取篇） + attempts（一次作答的记录）。
 *
 * - 取篇走 `/api/quiz/passages*`：做题阶段拿到的东西**不含答案**，答案只有判卷才回来。
 * - 作答记录走 `/api/reading/attempts*`：**这里是真相源**。localStorage 只作为
 *   旧版数据的迁移来源，迁移完不再写入（两处同时维护第二份真相是旧 bug 的根子）。
 * - 所有失败都返回 `{ ok: false, error }` 的人话错误，UI 据此显示并按409/500区分；
 *   绝不把失败伪装成"已保存"。
 */

import { questionIdOfNumber, type Structure } from "./reading";
import type {
  AttemptMode,
  PassageDetail,
  PassageMeta,
  QuestionResult,
  ReadingAttempt,
} from "./reading";

export type Outcome<T> = { ok: true; data: T } | { ok: false; error: string };

async function jsonOrError<T>(res: Response, fallback: string): Promise<Outcome<T>> {
  if (!res.ok) {
    const detail = await safeDetail(res);
    return { ok: false, error: detail || fallback };
  }
  try {
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, error: "后端返回的内容读不懂，稍后再试。" };
  }
}

async function safeDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: string };
    return typeof body.detail === "string" ? body.detail : "";
  } catch {
    return "";
  }
}

/**
 * attempt 类端点的解包：后端统一回信封 `{ ok, attempt }`，界面要的是**里面那个 attempt**。
 *
 * 为什么单列一个（BUG-01）：把信封整个交出去，下一次渲染 `attempt.notes.filter` 就会崩
 * （2026-10-12 真机七选五 100% 复现）。信封里没有 attempt（结构变了 / 后端坏了）时也不许
 * 把 `undefined` 当记录交出去，按人话错误处理——界面只会显示提示，不会整页灰屏。
 */
async function jsonAttempt(res: Response, fallback: string): Promise<Outcome<ReadingAttempt>> {
  const out = await jsonOrError<{ attempt?: ReadingAttempt }>(res, fallback);
  if (!out.ok) return out;
  const attempt = out.data?.attempt;
  if (!attempt || typeof attempt !== "object") {
    return { ok: false, error: "后端返回的内容读不懂，稍后再试。" };
  }
  return { ok: true, data: attempt };
}

// --------------------------------------------------------------- 篇章

export type PassageListOutcome = Outcome<{ passages: PassageMeta[] }>;

export async function listPassages(exam?: string): Promise<PassageListOutcome> {
  const q = exam?.trim() ? `?exam=${encodeURIComponent(exam.trim())}` : "";
  try {
    const res = await fetch(`/api/quiz/passages${q}`);
    return await jsonOrError(res, "精读台暂时打不开，稍后再试。");
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

export async function getPassage(quizId: string): Promise<Outcome<PassageDetail>> {
  try {
    const res = await fetch(`/api/quiz/passages/${encodeURIComponent(quizId)}`);
    return await jsonOrError(res, "这篇没取到，稍后再试。");
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

// --------------------------------------------------------------- 一次作答

export type AttemptPatch = {
  questions?: Array<{
    question_id: string;
    answer?: string;
    doubt?: boolean;
    candidates?: string[];
    excluded?: string[];
  }>;
  remove_questions?: string[];
  notes?: Array<{
    id: string;
    kind?: string;
    question_id?: string | null;
    anchor?: { block_id: string; start: number; end: number; quote?: string; passage_revision?: string } | null;
    body?: string;
    created_at?: string;
  }>;
  remove_notes?: string[];
  mode?: AttemptMode;
  deadline?: string | null;
  timed_out?: boolean;
  aids_used?: string[];
  session_id?: string;
};

export type AttemptCreate = {
  quiz_id: string;
  qtype: string;
  exam?: string | null;
  year?: string | null;
  source?: string | null;
  mode?: AttemptMode;
  session_id?: string;
  deadline?: string | null;
  passage_revision?: string;
  source_kind?: "live" | "legacy_migrated" | "legacy_summary";
  note_source?: string;
  questions?: AttemptPatch["questions"];
  notes?: AttemptPatch["notes"];
};

export async function createAttempt(body: AttemptCreate): Promise<Outcome<ReadingAttempt>> {
  try {
    const res = await fetch("/api/reading/attempts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return await jsonAttempt(res, "这次作答没能开始（本机没存上），再点一次试试。");
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

export async function getAttempt(attemptId: string): Promise<Outcome<ReadingAttempt>> {
  try {
    const res = await fetch(`/api/reading/attempts/${encodeURIComponent(attemptId)}`);
    return await jsonAttempt(res, "这次作答没读到，稍后再试。");
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

export async function patchAttempt(
  attemptId: string,
  patch: AttemptPatch,
): Promise<Outcome<ReadingAttempt>> {
  try {
    const res = await fetch(`/api/reading/attempts/${encodeURIComponent(attemptId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    return await jsonAttempt(res, "这一步没存上（草稿还在界面上），可以再试一次。");
  } catch {
    return { ok: false, error: "暂时连不上后端，这一步没存上。" };
  }
}

export async function submitAttempt(
  attemptId: string,
  answers: Record<string, string>,
  submitNo?: string,
): Promise<Outcome<ReadingAttempt>> {
  try {
    const res = await fetch(`/api/reading/attempts/${encodeURIComponent(attemptId)}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        submitNo ? { answers, submit_no: submitNo } : { answers },
      ),
    });
    return await jsonAttempt(res, "没判成。你的作答已经存好了，可以重试。");
  } catch {
    return { ok: false, error: "暂时连不上后端，判卷没成——作答已存好，联网后可重试。" };
  }
}

export type LatestOutcome =
  | { ok: true; attempt: ReadingAttempt | null }
  | { ok: false; error: string };

export async function latestAttempt(
  quizId: string,
  submittedOnly = false,
): Promise<LatestOutcome> {
  try {
    const res = await fetch(
      `/api/reading/attempts/latest?quiz_id=${encodeURIComponent(quizId)}`
        + (submittedOnly ? "&submitted_only=1" : ""),
    );
    if (!res.ok) {
      const detail = await safeDetail(res);
      return { ok: false, error: detail || "上次做到哪没读到，稍后再试。" };
    }
    const body = (await res.json()) as { found: boolean; attempt: ReadingAttempt | null };
    return { ok: true, attempt: body.found ? body.attempt : null };
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

export type AttemptSummary = {
  attempt_id: string;
  quiz_id: string;
  stage: string;
  updated_at: string | null;
  submitted_at: string | null;
  results: QuestionResult[];
};

export async function listAttempts(quizId?: string): Promise<Outcome<AttemptSummary[]>> {
  const q = quizId?.trim() ? `?quiz_id=${encodeURIComponent(quizId.trim())}` : "";
  try {
    const res = await fetch(`/api/reading/attempts${q}`);
    if (!res.ok) {
      const detail = await safeDetail(res);
      return { ok: false, error: detail || "做过的记录没读到，稍后再试。" };
    }
    const body = (await res.json()) as { attempts: AttemptSummary[] };
    return { ok: true, data: body.attempts ?? [] };
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

// --------------------------------------------------------------- 旧数据迁移

const LEGACY_PROGRESS_KEY = "markr.reading.progress";
const LEGACY_RESULT_KEY = "markr.reading.results";
const MIGRATION_FLAG = "glyph.reading.migrated.v2";

export type LegacySnapshot = {
  progress: Record<string, unknown> | null;
  results: Record<string, unknown> | null;
};

/**
 * 读旧版 localStorage（只读一次、读完打标记，不做写入）。
 *
 * 旧进度带 answered / doubts / notes（拼接成一次草稿迁移）；
 * 旧成绩只有 bestScore / wrongNumbers —— **那只是成绩摘要，不能据此声称原始作答还在**。
 */
export function readLegacySnapshot(): LegacySnapshot | null {
  if (typeof localStorage === "undefined") return null;
  if (localStorage.getItem(MIGRATION_FLAG)) return null;
  const parse = (key: string): Record<string, unknown> | null => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  };
  const progress = parse(LEGACY_PROGRESS_KEY);
  const results = parse(LEGACY_RESULT_KEY);
  if (!progress && !results) return null;
  return { progress, results };
}

type LegacyNote = {
  id?: string;
  kind?: string;
  text?: string;
  detail?: string;
  forQuestion?: number;
};

type LegacyProgress = {
  answers?: Record<string, string>;
  doubts?: string[];
  notes?: LegacyNote[];
};

/**
 * 旧版 localStorage 草稿 → 新记录的初始内容（**只搬真实存在的东西**）。
 *
 * 旧字段没有字符锚点（只有段号 / 段字母），所以笔记一律记成 **anchor: null**：
 * 与其猜一个可能错位的区间，不如老实标「这条笔记没有定位句」。
 */
export function legacyDraftPayload(structure: Structure, raw: unknown): AttemptCreate | null {
  if (!raw || typeof raw !== "object") return null;
  const p = raw as LegacyProgress;
  const answers = p.answers ?? {};
  const doubts = Array.isArray(p.doubts) ? p.doubts : [];
  const notes = Array.isArray(p.notes) ? p.notes : [];
  if (!Object.keys(answers).length && !doubts.length && !notes.length) return null;

  const questions = Object.entries(answers).map(([number, answer]) => ({
    question_id: questionIdOfNumber(structure, Number(number)),
    answer,
    doubt: doubts.includes(number),
  }));
  for (const number of doubts) {
    if (answers[number]) continue;
    questions.push({
      question_id: questionIdOfNumber(structure, Number(number)),
      answer: "",
      doubt: true,
    });
  }

  const migratedNotes = notes.map((n, i) => ({
    id: String(n.id ?? `legacy-${i}`),
    kind: n.kind === "word" || n.kind === "line" ? n.kind : "note",
    question_id: typeof n.forQuestion === "number" ? questionIdOfNumber(structure, n.forQuestion) : null,
    anchor: null,
    body: [n.text, n.detail].filter(Boolean).join("　").trim(),
  }));

  return {
    quiz_id: "",
    qtype: "",
    questions,
    notes: migratedNotes,
    source_kind: "legacy_migrated",
    note_source: LEGACY_PROGRESS_KEY,
  };
}

export function markLegacyMigrated(): void {
  try {
    localStorage.setItem(MIGRATION_FLAG, "1");
  } catch {
    /* 迁移标记写不进去最多是下次再问一次，不影响做题 */
  }
}
