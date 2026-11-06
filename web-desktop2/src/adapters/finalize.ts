/**
 * 定稿（拍板 → /api/grade/finalize 请求体）纯逻辑。
 *
 * 后端契约（web/server.py FinalizeRequest）：
 * - decisions：每条 detection 一个表态 accepted / rejected / edited / cancelled；
 * - overrides：edited 时用户自己的改法（id → 文本）；
 * - edited_ids：手改过的条目 id；
 * - 没表态的条目后端按撤销处理，所以这里对「没拍板」显式补 rejected，语义一致。
 */
import type { DetectionDTO } from "./batch";

export type DetectionAction = "accepted" | "edited" | "rejected";
export type DetectionDecisionMap = Record<string, { action: DetectionAction; override?: string }>;

export type FinalizePayload = {
  text: string;
  detections: DetectionDTO[];
  decisions: Record<string, "accepted" | "rejected" | "edited">;
  overrides: Record<string, string>;
  edited_ids: string[];
  comment: string;
  progress: { decided: number; total: number; kept: number; allDecided: boolean };
};

export function buildFinalizePayload(input: {
  text: string;
  detections: DetectionDTO[];
  decisions: DetectionDecisionMap;
  comment?: string;
}): FinalizePayload {
  const { text, detections, decisions, comment = "" } = input;
  const out_decisions: FinalizePayload["decisions"] = {};
  const overrides: Record<string, string> = {};
  const edited_ids: string[] = [];

  let decided = 0;
  let kept = 0;

  for (const d of detections) {
    const id = (d.id ?? "").toString();
    if (!id) continue; // 没有 id 的条无法表态，原样留在 detections 里即可
    const user = decisions[id];
    if (user) decided += 1;

    let action: "accepted" | "rejected" | "edited";
    if (!user) {
      action = "rejected"; // 没拍板 = 不保留
    } else if (user.action === "edited" && (user.override ?? "").trim() !== "") {
      action = "edited";
      overrides[id] = user.override!.trim();
      edited_ids.push(id);
    } else if (user.action === "rejected") {
      action = "rejected";
    } else {
      // accepted，或 edited 却没给改法 → 按采纳原改法
      action = "accepted";
    }
    out_decisions[id] = action;
    if (action === "accepted" || action === "edited") kept += 1;
  }

  const total = detections.filter((d) => (d.id ?? "").toString() !== "").length;

  return {
    text,
    detections,
    decisions: out_decisions,
    overrides,
    edited_ids,
    comment,
    progress: { decided, total, kept, allDecided: decided === total },
  };
}

/** 后端 build_marked_segments 的分段（model_dump 后）：plain/removed/added */
export type MarkSeg = {
  kind: "plain" | "removed" | "added";
  text: string;
  index?: number;
  detection_id?: string | null;
};

export function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * 定稿卡正文：在**定稿全文**上把改后片段（marks 里的 added）顺序高亮成绿色。
 * marks 是相对原文的批阅分段（removed/added），定稿全文里没有原文片段，
 * 所以只取 added 文本，用游标按批改顺序在定稿全文中定位——同一个短语出现
 * 多次时不会标错位置；定位不到就跳过（数据对不上时宁可不标）。
 */
export function renderFinalText(finalText: string, marks: MarkSeg[] | undefined): string {
  if (!marks || marks.length === 0) return escapeHtml(finalText).replace(/\n/g, "<br>");
  let out = "";
  let cursor = 0;
  for (const m of marks) {
    if (m.kind !== "added" || !m.text) continue;
    const idx = finalText.indexOf(m.text, cursor);
    if (idx < 0) continue;
    out += escapeHtml(finalText.slice(cursor, idx));
    out += `<mark class="dd-hl">${escapeHtml(m.text)}</mark>`;
    cursor = idx + m.text.length;
  }
  out += escapeHtml(finalText.slice(cursor));
  return out.replace(/\n/g, "<br>");
}
