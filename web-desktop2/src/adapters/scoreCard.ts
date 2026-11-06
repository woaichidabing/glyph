/* ============================================================
 * 写作四维评分（内容 / 结构 / 准确 / 丰富）展示逻辑。
 * 纯解析与档位计算，不碰 DOM；后端已收敛过分数，这里只认真数字。
 * ============================================================ */

export type DimensionDTO = { score: number | null; comment_zh: string };

export type ScoresDTO = {
  content: DimensionDTO;
  structure: DimensionDTO;
  accuracy: DimensionDTO;
  richness: DimensionDTO;
};

export type ScoreBand = "high" | "mid" | "low" | "bad" | "none";

/** 固定展示顺序与中文名 */
export const DIMENSIONS: { key: keyof ScoresDTO; label: string }[] = [
  { key: "content", label: "内容" },
  { key: "structure", label: "结构" },
  { key: "accuracy", label: "准确" },
  { key: "richness", label: "丰富" },
];

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function clampScore(v: unknown): number | null {
  if (typeof v !== "number" || !Number.isFinite(v)) return null;
  return Math.max(0, Math.min(100, Math.round(v)));
}

/** 从 grade 结果的 scores 块解析；整块缺失 / 四维全空 → null（不渲染分数区）。 */
export function extractScores(raw: unknown): ScoresDTO | null {
  if (!isRecord(raw)) return null;
  const result = {} as ScoresDTO;
  let any = false;
  for (const { key } of DIMENSIONS) {
    const node = raw[key];
    let score: number | null = null;
    let comment = "";
    if (isRecord(node)) {
      score = clampScore(node.score);
      if (score !== null) any = true;
      if (typeof node.comment_zh === "string") comment = node.comment_zh;
    }
    result[key] = { score, comment_zh: comment };
  }
  return any ? result : null;
}

/** 配色 / 语义档位：≥75 好、60-74 中、40-59 偏弱、<40 差、null 不适用 */
export function scoreBand(score: number | null): ScoreBand {
  if (score === null) return "none";
  if (score >= 75) return "high";
  if (score >= 60) return "mid";
  if (score >= 40) return "low";
  return "bad";
}

/** 可用维度的平均分（忽略 null 维度）；全 null 时为 null */
export function averageScore(s: ScoresDTO): number | null {
  const vals = DIMENSIONS.map((d) => s[d.key].score).filter(
    (x): x is number => x !== null,
  );
  if (vals.length === 0) return null;
  return Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
}
