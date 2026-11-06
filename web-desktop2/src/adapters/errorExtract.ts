/* ============================================================
 * 作文定稿 → 错词本提炼（纯函数，可单测）
 *
 * 口径（宁收准、不收滥，错词本是背单词形态）：
 *  1. 只收硬错（tier=error / type=hard），润色建议不收；
 *  2. 只收**单个英文词**：两词以上的片段（I learned / feel relaxed 这类）不进错词本——
 *     错词本按词查词典，短语查不到释义，只会变成一条哑条目；搭配问题留给批改讲解；
 *  3. 同一正确形式一篇内只收一次（addError 还会按词全局去重）；
 *  4. 词条背的是正确形式（went，不是 goes），句首大写归一为小写；
 *  5. 句子语境尽量从原文坐标切，切不到就留空（不编造语境）。
 * ============================================================ */
import type { DetectionDTO } from "./batch";

export type ExtractedError = {
  word: string;
  wrong: string;
  sentence: string;
  reason: string;
  from: string;
};

const SENTENCE_ENDS = [".", "!", "?", "。", "！", "？"];
/** 句子边界：句末标点 + 换行（OCR 文本的标题行、段落都靠换行分隔，不能跨进去） */
const BOUNDARIES = [...SENTENCE_ENDS, "\n", "\r"];
const MAX_WORDS = 1;
const MAX_REASON = 80;

/** 正确形式是否为 1~2 个英文词（允许撇号缩写 don't / 连字符） */
function wordLevel(corrected: string): string[] | null {
  const cleaned = corrected.replace(/[.,;:!?"()【】、，。；：！？]/g, " ").trim();
  const words = cleaned.split(/\s+/).filter(Boolean);
  if (words.length < 1 || words.length > MAX_WORDS) return null;
  if (!words.every((w) => /^[A-Za-z][A-Za-z'\-]*$/.test(w) && w.length <= 25)) return null;
  return words;
}

/** 句首大写归一：Went → went；USA / 全大写专有名词不动 */
function normalizeWord(w: string): string {
  if (/^[A-Z][a-z]+$/.test(w)) return w.toLowerCase();
  return w;
}

function sentenceAround(text: string | undefined, start: number | null | undefined, end: number | null | undefined): string {
  if (!text || start == null || end == null || start < 0 || end <= start) return "";
  let left = -1;
  for (const mark of BOUNDARIES) {
    const idx = text.lastIndexOf(mark, start - 1);
    if (idx > left) left = idx;
  }
  let right = -1;
  for (const mark of BOUNDARIES) {
    const idx = text.indexOf(mark, end);
    if (idx >= 0 && (right < 0 || idx < right)) right = idx;
  }
  return text.slice(left + 1, right < 0 ? undefined : right + 1).trim();
}

export function extractErrorsFromFinalize(args: {
  kept: DetectionDTO[];
  sourceText?: string;
  today: string;
}): ExtractedError[] {
  const { kept, sourceText, today } = args;
  const out: ExtractedError[] = [];
  const seen = new Set<string>();

  for (const d of kept) {
    const original = (d.original ?? "").trim();
    const correctedRaw = (d.corrected ?? "").trim();
    if (!original || !correctedRaw) continue;
    // 润色建议不收（硬错口径：tier=error 或旧字段 type=hard）
    const isHard = d.tier === "error" || d.type === "hard";
    if (!isHard) continue;
    const words = wordLevel(correctedRaw);
    if (!words) continue;
    const word = words.map(normalizeWord).join(" ");
    if (seen.has(word.toLowerCase())) continue;
    seen.add(word.toLowerCase());

    const reason = (d.explanation_zh ?? "").trim()
      || `作文里写成了 ${original}，正确是 ${correctedRaw}`;
    out.push({
      word,
      wrong: original,
      sentence: sentenceAround(sourceText, d.start, d.end),
      reason: reason.length > MAX_REASON ? `${reason.slice(0, MAX_REASON)}…` : reason,
      from: `${today} 作文批改`,
    });
  }
  return out;
}
