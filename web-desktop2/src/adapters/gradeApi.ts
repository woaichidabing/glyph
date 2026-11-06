/**
 * 批改定稿与复核配额接口（直连后端，不经过模型流）。
 * - POST /api/grade/finalize：拍板是人的动作，后端只做纯文本套用，不调模型；
 * - GET  /api/recheck/quota：每份批改的复核配额（默认每篇/总量各有限额）；
 * - POST /api/recheck/reset：新一批批改出来时清账本（后端装配层也会管，这里兜底）。
 */
import type { DetectionDTO } from "./batch";

export type FinalizeRequest = {
  text: string;
  detections: DetectionDTO[];
  decisions: Record<string, "accepted" | "rejected" | "edited" | "cancelled">;
  overrides?: Record<string, string>;
  edited_ids?: string[];
  comment?: string;
  session_id?: string | null;
  grade_call_id?: string | null;
};

export type FinalizeResponse = {
  final_text: string;
  result_text: string;
  marks: unknown[];
  kept: DetectionDTO[];
  warnings: string[];
  /** appended=已落会话流水；skipped_*=没落（原因），定稿内容仍照常返回 */
  finalize_record: string;
};

async function asJson<T>(res: Response): Promise<T> {
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON */
  }
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${res.status}）`);
  }
  return data as unknown as T;
}

export async function finalizeGrade(req: FinalizeRequest): Promise<FinalizeResponse> {
  const res = await fetch("/api/grade/finalize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ comment: "", edited_ids: [], ...req }),
  });
  return asJson<FinalizeResponse>(res);
}

export type RecheckQuota = {
  per_item_max: number;
  total_max: number;
  total_used: number;
  total_remaining: number;
  per_item_used: Record<string, number>;
  per_item_remaining: Record<string, number>;
};

export async function getRecheckQuota(): Promise<RecheckQuota | null> {
  try {
    const res = await fetch("/api/recheck/quota");
    if (!res.ok) return null;
    return (await res.json()) as RecheckQuota;
  } catch {
    return null; // 后端没起/老版本：UI 兜底按本地计数
  }
}

export async function resetRecheck(): Promise<void> {
  try {
    await fetch("/api/recheck/reset", { method: "POST" });
  } catch {
    /* 配额账本是辅助功能，失败不挡批改 */
  }
}

/* ---------- 台内直连复核（不经过对话） ---------- */

export type RecheckResult = {
  id: string;
  status: "upheld" | "withdrawn" | "revised";
  round: number;
  scope?: string;
  corrected?: string | null;
  explanation_zh?: string | null;
  tier?: string | null;
  type?: string | null;
  note_zh?: string;
  reason_zh?: string;
  warnings?: string[];
};

export type RecheckApiResponse = {
  ok: true;
  recheck: RecheckResult;
  quota: RecheckQuota;
};

/** 批改台点「驳回」：直连复核这一条，理由可空。失败抛带中文 detail 的 Error。 */
export async function recheckDetection(req: {
  text: string;
  detection: DetectionDTO;
  note?: string;
}): Promise<RecheckApiResponse> {
  const res = await fetch("/api/recheck", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: req.text, detection: req.detection, note: req.note ?? "" }),
  });
  return asJson<RecheckApiResponse>(res);
}
