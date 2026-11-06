/**
 * 内联改稿（Word 接受修订式）纯函数：把批改结果 + 用户逐条决定 + 台内复核结论，
 * 算成「带痕迹的正文段」「逐卡片视图模型」「改后全文」「进度」。
 *
 * 边界：
 * - 不联网、不碰 React、不读 store——输入定死，输出定死，离线可测；
 * - 区间口径与后端 review.py 对齐：aligned=false / 缺 start/end / 越界 / 与前一处
 *   重叠的条不进正文（避免改错位置），落进 loose 卡片在正文后单独列；
 * - 复核「撤销」视同驳回（原文保留、不进改后全文），但它来自模型复核而非用户决定。
 */
import type { DetectionDTO } from "./batch";

export type DecisionAction = "accepted" | "rejected" | "edited";
export type GradeDecision = { action: DecisionAction; override?: string };
export type RecheckStatus = "upheld" | "withdrawn" | "revised";

/** /api/recheck 返回的结论（只取渲染需要的字段） */
export type RecheckVerdictLike = {
  status: RecheckStatus;
  corrected?: string | null;
  explanation_zh?: string | null;
  tier?: string | null;
  type?: string | null;
  round?: number;
  reason_zh?: string;
  note_zh?: string;
};

export type EffectiveView = {
  original: string;
  corrected?: string;
  explanation_zh?: string;
  tier?: string;
  type?: string;
};

export type InlineCard = {
  id: string;
  /** 1 起编号，按 detections 数组顺序（模型按原文位置给出） */
  index: number;
  /** 能不能挂进正文；false = 定位不到，在正文后用 loose 卡片列 */
  inline: boolean;
  det: DetectionDTO;
  /** 拍板时实际生效的原文/改法/讲解（复核改判后会变） */
  effective: EffectiveView;
  decision?: GradeDecision;
  recheckStatus?: RecheckStatus;
  recheckRound?: number;
  recheckReason?: string;
  recheckNote?: string;
  /** 已撤销（复核结论）或用户已表态 */
  decided: boolean;
  /** 会不会进改后全文：采纳 / 自己改；驳回与撤销都不进 */
  kept: boolean;
};

export type InlineSegment =
  | { kind: "plain"; text: string }
  | { kind: "pending"; text: string; detId: string; index: number }
  | { kind: "accepted"; oldText: string; newText: string; detId: string; mine: boolean };

export type InlineEssay = {
  segments: InlineSegment[];
  cards: InlineCard[];
  /** 定位不到、没挂进正文的卡片 id（按数组顺序） */
  looseIds: string[];
  progress: { total: number; decided: number; accepted: number; edited: number; rejected: number };
  /** 所有条都有归宿：采纳 / 自己改 / 驳回 / 复核撤销 */
  allDecided: boolean;
  /** 采纳与自己改已套入的全文（未处理/驳回的位置保持原文），供「复制改后版」 */
  changedText: string;
};

function isUsable(text: string, d: DetectionDTO): boolean {
  if (d.aligned === false) return false;
  const { start, end } = d;
  return typeof start === "number" && typeof end === "number"
    && Number.isInteger(start) && Number.isInteger(end)
    && start >= 0 && end > start && end <= text.length;
}

function effectiveOf(d: DetectionDTO, verdict?: RecheckVerdictLike): EffectiveView {
  const revised = verdict?.status === "revised";
  return {
    original: d.original ?? "",
    corrected: revised && verdict.corrected ? verdict.corrected : d.corrected ?? undefined,
    explanation_zh: revised && verdict.explanation_zh
      ? verdict.explanation_zh
      : d.explanation_zh ?? undefined,
    tier: revised ? verdict.tier ?? d.tier : d.tier,
    type: revised ? verdict.type ?? d.type : d.type,
  };
}

/**
 * 把复核「改判」结论合并回 detection 列表，供 /api/grade/finalize 使用。
 * 后端定稿只认请求体里的 detections + decisions——不合并的话，界面上按新建议
 * 采纳，定稿套的还是旧改法（界面与定稿漂移）。revised 没带的字段保留原值；
 * upheld / withdrawn / 无 verdict 原样返回（withdrawn 由 decisions 侧剔除）。
 */
export function detectionsWithVerdicts(
  dets: DetectionDTO[],
  verdicts: Record<string, RecheckVerdictLike>,
): DetectionDTO[] {
  return dets.map((d) => {
    const v = d.id ? verdicts[d.id] : undefined;
    if (v?.status !== "revised") return d;
    return {
      ...d,
      corrected: v.corrected ?? d.corrected,
      explanation_zh: v.explanation_zh ?? d.explanation_zh,
      tier: v.tier ?? d.tier,
      type: v.type ?? d.type,
    };
  });
}

/**
 * 一张待拍板卡片上「驳回」相关的两个出口：
 * - canAskModel：还能让模型再复核（按钮文案走 recheckLabel）；
 * - canKeepOriginal：不调模型、直接按驳回保持原文的出口。
 * 规矩：首次驳回必须先听模型第二意见；模型给过维持/改判、配额用尽或请求失败时，
 * 人必须有最终裁定出口——否则卡片永远没有 rejected 归宿，「看完了」被卡死。
 */
export type RecheckGate = {
  canAskModel: boolean;
  canKeepOriginal: boolean;
  recheckLabel: string;
};

export function recheckOptions(
  card: InlineCard,
  /** 本条剩余复核次数；null = 后端不可达/老版本，不拦 */
  quotaRemaining: number | null,
  /** 本轮复核请求是否刚失败（网络 / 502 / 409 已同步成 0） */
  failed: boolean,
): RecheckGate {
  const canAskModel = quotaRemaining === null || quotaRemaining > 0;
  const hasVerdict = card.recheckStatus === "upheld" || card.recheckStatus === "revised";
  const canKeepOriginal = hasVerdict || quotaRemaining === 0 || failed;
  // 首次驳回只写「驳回」（还没复核过，挂剩余次数是噪音）；再驳回才带剩几次
  let recheckLabel: string;
  if (!card.recheckStatus) recheckLabel = "驳回";
  else if (quotaRemaining === null) recheckLabel = "再驳回";
  else recheckLabel = `再驳回（剩 ${quotaRemaining} 次）`;
  return { canAskModel, canKeepOriginal, recheckLabel };
}

export function buildInlineEssay(
  text: string,
  detections: DetectionDTO[],
  decisions: Record<string, GradeDecision>,
  verdicts: Record<string, RecheckVerdictLike> = {},
): InlineEssay {
  // 1) 卡片视图：先算有效字段与归宿状态
  const cards: InlineCard[] = detections.map((d, i) => {
    // 缺 id 时用 det-N 兜底：卡片、表态、复核都认这一个 id，
    // 否则「全部采纳」拿不到 id、按钮永远禁用（2026-11-02 真机：三篇批改后点不动）。
    const id = d.id ?? `det-${i + 1}`;
    const verdict = verdicts[id];
    const decision = decisions[id];
    const withdrawn = verdict?.status === "withdrawn";
    const kept = decision?.action === "accepted" || decision?.action === "edited";
    return {
      id,
      index: i + 1,
      inline: isUsable(text, d),
      det: d,
      effective: effectiveOf(d, verdict),
      decision,
      recheckStatus: verdict?.status,
      recheckRound: verdict?.round,
      recheckReason: verdict?.reason_zh,
      recheckNote: verdict?.note_zh,
      decided: kept || decision?.action === "rejected" || withdrawn,
      kept,
    };
  });

  // 2) 可挂正文的区间，按位置排序；重叠的后一条退回 loose（与后端同口径）
  const placed = cards
    .filter((c) => c.inline && typeof c.det.start === "number" && typeof c.det.end === "number")
    .sort((a, b) => (a.det.start as number) - (b.det.start as number));
  const chosen: InlineCard[] = [];
  let lastEnd = -1;
  for (const c of placed) {
    const s = c.det.start as number;
    const e = c.det.end as number;
    if (s < lastEnd) {
      c.inline = false; // 与前一处重叠：不挂正文
      continue;
    }
    chosen.push(c);
    lastEnd = e;
  }
  const looseIds = cards.filter((c) => !c.inline).map((c) => c.id);

  // 3) 段落 + 改后全文
  const segments: InlineSegment[] = [];
  const changedParts: string[] = [];
  let cursor = 0;
  for (const c of chosen) {
    const s = c.det.start as number;
    const e = c.det.end as number;
    const oldText = text.slice(s, e);
    if (s > cursor) {
      segments.push({ kind: "plain", text: text.slice(cursor, s) });
      changedParts.push(text.slice(cursor, s));
    }

    if (c.kept) {
      const mine = c.decision?.action === "edited";
      const replacement = (mine ? c.decision?.override : c.effective.corrected) ?? oldText;
      segments.push({ kind: "accepted", oldText, newText: replacement, detId: c.id, mine });
      changedParts.push(replacement);
    } else {
      // 未处理 / 驳回 / 复核撤销：原文照旧；未处理的挂 pending 痕迹
      if (!c.decided) {
        segments.push({ kind: "pending", text: oldText, detId: c.id, index: c.index });
      } else {
        segments.push({ kind: "plain", text: oldText });
      }
      changedParts.push(oldText);
    }
    cursor = e;
  }
  if (cursor < text.length) {
    segments.push({ kind: "plain", text: text.slice(cursor) });
    changedParts.push(text.slice(cursor));
  }

  // 4) 进度
  const count = (a: DecisionAction) => cards.filter((c) => c.decision?.action === a).length;
  const decided = cards.filter((c) => c.decided).length;
  const progress = {
    total: cards.length,
    decided,
    accepted: count("accepted"),
    edited: count("edited"),
    rejected: count("rejected"),
  };

  return {
    segments,
    cards,
    looseIds,
    progress,
    allDecided: decided === cards.length,
    changedText: changedParts.join(""),
  };
}
