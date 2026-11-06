/**
 * 题库库：把后端散卡聚合好的「套」列表接进来，供题库页展示与删除。
 *
 * - 列表 GET /api/quiz/list、删除 POST /api/quiz/papers/delete（单套 / 批量同口）。
 * - 套的展示纯函数（标题 / 分组 / 篇数）不碰网络，vitest 直测；IO 函数靠真机验。
 *
 * 字段保持后端原样（snake_case），列表回来就能直接喂给纯函数 / UI，不多一层易出错的改名。
 */

/** 后端题型标签的固定展示顺序（卷子里的四块） */
export const QTYPE_ORDER = ["仔细阅读", "段落匹配", "选词填空", "写作"] as const;

/** 一套卷子的汇总（GET /api/quiz/list 的单项，字段与后端一致） */
export type PaperSummary = {
  key: string;
  exam: string | null;
  year: string | null;
  /** 套次名（第 1 套 …）；批量导入共用来源表单时可能为空，由 paperTitle 用 key 兜底 */
  source: string | null;
  /** 题型 → 篇（卡）数，缺的题型就是没有，按 0 处理 */
  counts: Record<string, number>;
  card_count: number;
  /** 出厂随包、只读，不可删 */
  builtin: boolean;
  updated_at: string | null;
};

// --------------------------------------------------------------- 套展示纯函数

const _CN_NUM: Record<string, string> = {
  一: "1", 二: "2", 三: "3", 四: "4", 五: "5", 六: "6",
};
/** key 里的英文套次：cet4-2026-set2 / xxx-set-2 */
const _SET_EN_RE = /(?:^|[-_])set[-\s]?(\d+)\b/i;
/** key 里的中文套次：…第2套 / …第三套 */
const _SET_CN_RE = /第\s*([0-9一二三四五六])\s*套/;

/**
 * 一套卷子在列表里叫什么：
 * 1) 来源标签非空（单套导入时识别 / 手填的「第1套」）直接用；
 * 2) 否则从文件名 key 的 setN / 第N套 还原；
 * 3) 都没有就用 key 原文（高考那种整卷命名）。
 */
export function paperTitle(p: PaperSummary): string {
  const source = p.source?.trim();
  if (source) return source;

  const en = _SET_EN_RE.exec(p.key);
  if (en) return `第${en[1]}套`;
  const cn = _SET_CN_RE.exec(p.key);
  if (cn) return `第${_CN_NUM[cn[1]] ?? cn[1]}套`;

  return p.key;
}

function formatYear(year: string | null | undefined): string {
  const t = (year ?? "").trim();
  if (!t) return "";
  return /^\d{4}$/.test(t) ? `${t}年` : t;
}

/** 分组标题：「四级 · 2026年」；只有考试就只显考试；都没有归「未分组」 */
export function paperGroupTitle(p: PaperSummary): string {
  const exam = p.exam?.trim();
  const year = formatYear(p.year);
  if (exam && year) return `${exam} · ${year}`;
  if (exam) return exam;
  if (year) return year;
  return "未分组";
}

export type CountChip = { label: string; n: number };

/** 这套各题型有几篇，按固定顺序、跳过没有的题型 */
export function countsChips(p: PaperSummary): CountChip[] {
  const chips: CountChip[] = [];
  for (const label of QTYPE_ORDER) {
    const n = p.counts?.[label] ?? 0;
    if (n > 0) chips.push({ label, n });
  }
  return chips;
}

/** 一行人话：「仔细阅读 2 · 段落匹配 1 · 写作 1」；一张没有给空串 */
export function countsText(p: PaperSummary): string {
  return countsChips(p).map(c => `${c.label} ${c.n}`).join(" · ");
}

export type PaperGroup = { title: string; papers: PaperSummary[] };

/**
 * 按「考试 · 年份」把套分组。后端列表已按最近导入倒序，这里保持首次出现顺序建组、
 * 组内沿用原顺序，不再二次排序，避免和后端的时间口径打架。
 */
export function groupPapers(papers: PaperSummary[]): PaperGroup[] {
  const order: string[] = [];
  const map = new Map<string, PaperSummary[]>();
  for (const p of papers) {
    const title = paperGroupTitle(p);
    const arr = map.get(title);
    if (arr) {
      arr.push(p);
    } else {
      map.set(title, [p]);
      order.push(title);
    }
  }
  return order.map(title => ({ title, papers: map.get(title)! }));
}

// --------------------------------------------------------------- 列表 / 删除 IO

export type PaperListOutcome =
  | { ok: true; papers: PaperSummary[]; skippedFiles: number }
  | { ok: false; error: string };

/** 拉取题库套卷列表（出厂只读 + 用户导入）。exam 非空时按考试过滤。 */
export async function listPapers(exam?: string): Promise<PaperListOutcome> {
  const q = exam?.trim() ? `?exam=${encodeURIComponent(exam.trim())}` : "";
  try {
    const res = await fetch(`/api/quiz/list${q}`);
    if (!res.ok) return { ok: false, error: await readDetail(res, "题库暂时打不开，稍后再试。") };
    const data = (await res.json()) as {
      papers?: PaperSummary[];
      skipped_files?: number;
    };
    return { ok: true, papers: data.papers ?? [], skippedFiles: data.skipped_files ?? 0 };
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

export type DeletedPaper = { key: string; count: number };

export type DeleteOutcome =
  | ({ ok: true } & {
      deleted: DeletedPaper[];
      /** 出厂只读、没删成的套 */
      denied: string[];
      /** 没找到的套 */
      missing: string[];
      count: number;
      message: string;
    })
  | { ok: false; error: string };

/** 按套删用户导入的题卡（单套传一个 key，批量传多个）；出厂套会被后端归到 denied。 */
export async function deletePapers(keys: string[]): Promise<DeleteOutcome> {
  try {
    const res = await fetch("/api/quiz/papers/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }),
    });
    if (!res.ok) return { ok: false, error: await readDetail(res, "没删成，请稍后再试。") };
    const data = (await res.json()) as {
      deleted?: DeletedPaper[];
      denied?: string[];
      missing?: string[];
      count?: number;
      message?: string;
    };
    return {
      ok: true,
      deleted: data.deleted ?? [],
      denied: data.denied ?? [],
      missing: data.missing ?? [],
      count: data.count ?? 0,
      message: data.message ?? "",
    };
  } catch {
    return { ok: false, error: "暂时连不上后端，稍后再试一次。" };
  }
}

async function readDetail(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown; message?: unknown };
    if (typeof data.detail === "string" && data.detail) return data.detail;
    if (typeof data.message === "string" && data.message) return data.message;
  } catch {
    /* 响应不是 JSON，走兜底 */
  }
  return fallback;
}
