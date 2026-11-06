/* ============================================================
 * propose_wordbook_add 工具结果 → 收词确认卡数据（纯解析，不碰存储 / DOM）。
 * 工具只负责“提议”，真正收藏在组件里走 useWordbook.add（用户点按钮后）。
 * ============================================================ */

export type WordbookPhonetic = { uk?: string; us?: string };

export type WordbookProposal = {
  word: string;
  phonetic?: WordbookPhonetic;
  pos: string;
  gloss: string;
  /** 工具返回时的查词次数快照；组件还会用实时 store 取 max */
  lookedUpCount: number;
  /** 用户查的是变形（went）而词条是原形（go）时带回，卡片可提一句 */
  queriedForm?: string;
};

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 兼容 SSE（对象）与历史回放（JSON 字符串）两种 tool_result.output。 */
function asRecord(output: unknown): Record<string, unknown> | null {
  if (isRecord(output)) return output;
  if (typeof output !== "string") return null;
  try {
    const parsed: unknown = JSON.parse(output);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * 从 propose_wordbook_add 的 tool_result.output 解析收词卡。
 * proposed 不为 true、或 status 明确不是 proposed（未收录 / 已收藏 / 无释义）
 * 都返回 null；调用方据此退回普通工具行，不渲染一张空卡。
 */
export function extractWordbookProposal(output: unknown): WordbookProposal | null {
  const data = asRecord(output);
  if (!data) return null;
  if (data.proposed !== true) return null;
  if (typeof data.status === "string" && data.status !== "proposed") return null;

  const word = typeof data.word === "string" ? data.word.trim() : "";
  const gloss = typeof data.gloss === "string" ? data.gloss.trim() : "";
  if (!word || !gloss) return null;

  const rawPhonetic = isRecord(data.phonetic) ? data.phonetic : null;
  const uk = typeof rawPhonetic?.uk === "string" ? rawPhonetic.uk.trim() : "";
  const us = typeof rawPhonetic?.us === "string" ? rawPhonetic.us.trim() : "";
  const phonetic: WordbookPhonetic | undefined =
    uk || us ? { ...(uk ? { uk } : {}), ...(us ? { us } : {}) } : undefined;

  const rawCount = data.lookedUpCount;
  const lookedUpCount =
    typeof rawCount === "number" && Number.isFinite(rawCount) && rawCount > 0
      ? Math.floor(rawCount)
      : 0;

  const pos = typeof data.pos === "string" ? data.pos.trim() : "";
  const queriedForm =
    typeof data.queriedForm === "string" && data.queriedForm.trim()
      ? data.queriedForm.trim()
      : undefined;

  return {
    word,
    ...(phonetic ? { phonetic } : {}),
    pos,
    gloss,
    lookedUpCount,
    ...(queriedForm ? { queriedForm } : {}),
  };
}
