/**
 * 用量与「省了多少」的推导：只读时间线、不改状态（纯函数，vitest 直接测）。
 *
 * 为什么单开一个文件：发送区状态条和"这一轮没省到"那句解释必须共用同一份口径。
 * 口径写两遍迟早漂移——一边按 token 说、一边按钱说，用户会看到两个百分比打架。
 *
 * 口径（重要）：
 * - 平台对**命中公共前缀**的输入按标准价 2 折计费，所以"省了多少"是**按钱**算的：
 *   省下 = 0.8 × 命中率。命中率是 token 口径，两个数不是一回事，界面要说清。
 * - 命中缓存的前提是前缀够长（平台文档：1024 token 起）。低于这个量本来就不具备
 *   命中条件，不能拿来跟用户解释"你这次没省到"。
 * - 模型没回缓存明细时一律"未知"：不给百分比、不提示，绝不拿 0 顶替（0 = 一个都没命中）。
 */

import type { TimelineNode } from "./askTimeline";

/** 命中部分按标准价 2 折计费 → 这部分省下 80%。 */
export const CACHE_SAVED_FACTOR = 0.8;

/** 平台缓存的最小前缀（token）：低于它本来就没资格命中，不解释。 */
export const CACHE_MIN_PREFIX_TOKENS = 1024;

/** 命中率低于这个值才算「这一轮没省到」，值得解释一句。 */
export const MISS_HIT_PCT = 30;

/** 第一轮没省到：说成正常现象，别吓唬人。 */
export const MISS_FIRST_TURN = "新会话刚开始，还没有可复用的前缀";
/** 后面某轮没省到：多半是上下文那块变了（写了记忆 / 跨了零点）。 */
export const MISS_PREFIX_CHANGED = "公共前缀变了（比如刚更新过记忆）";

export type UsageSummary = {
  /** 用户问了几轮 */
  turns: number;
  /** 工具跑了几步 */
  steps: number;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
  /** token 口径的命中率（0-100 整数）；没有缓存明细时为 null（未知，不是 0） */
  hitPct: number | null;
  /** 按钱算省了多少（0-100 整数）；同样可能为 null */
  savedPct: number | null;
};

/** 历史会话的用量合计（后端从流水里加出来的一段）。切回旧会话时状态条靠它回显。 */
export type UsageBaseline = {
  inputTokens: number;
  outputTokens: number;
  /** null = 会话里一轮都没记过缓存明细（未知），不是 0 */
  cachedTokens: number | null;
};

export type UsageView = {
  summary: UsageSummary;
  /** 终端节点 id → 那句解释；只有"这一轮没省到"的轮次才在里面 */
  missNotes: Map<string, string>;
};

const EMPTY: UsageSummary = {
  turns: 0,
  steps: 0,
  inputTokens: 0,
  outputTokens: 0,
  cachedTokens: 0,
  hitPct: null,
  savedPct: null,
};

/**
 * 把时间线投影成"用量视图"：状态条要的汇总 + 该解释哪几轮。
 *
 * `baseline` 是**历史会话的合计**（后端从流水里加出来的）：切回旧会话时逐轮明细不在
 * 时间线里，只有这个合计数——不带上它状态条会整行消失，用户会以为白省了。
 * 实时那几轮照旧逐轮累加，两者相加。
 */
export function deriveUsage(
  nodes: TimelineNode[],
  baseline?: UsageBaseline | null,
): UsageView {
  let turns = 0;
  let steps = 0;
  let inputTokens = baseline?.inputTokens ?? 0;
  let outputTokens = baseline?.outputTokens ?? 0;
  let cachedTokens = baseline?.cachedTokens ?? 0;
  let sawCached = baseline != null && baseline.cachedTokens != null;
  /** 已经量到用量的轮数：只用它判"这是不是第一轮" */
  let measuredTurns = 0;

  const missNotes = new Map<string, string>();
  const toldReasons = new Set<string>();

  for (const node of nodes) {
    if (node.kind === "user") {
      turns += 1;
      continue;
    }
    if (node.kind === "tool") {
      steps += 1;
      continue;
    }
    if (node.kind !== "terminal" || !node.usage) continue;

    measuredTurns += 1;
    const input = node.usage.inputTokens;
    const cached = node.usage.cachedTokens;
    inputTokens += input;
    outputTokens += node.usage.outputTokens;
    if (cached === null) continue; // 未知：只累计，不解释
    sawCached = true;
    cachedTokens += cached;

    if (input < CACHE_MIN_PREFIX_TOKENS) continue; // 没到平台门槛，本就没资格命中
    if ((cached / input) * 100 >= MISS_HIT_PCT) continue;

    const reason = measuredTurns === 1 ? MISS_FIRST_TURN : MISS_PREFIX_CHANGED;
    if (toldReasons.has(reason)) continue; // 同一个原因一个会话只念一次
    toldReasons.add(reason);
    missNotes.set(node.id, reason);
  }

  const summary: UsageSummary = {
    ...EMPTY,
    turns,
    steps,
    inputTokens,
    outputTokens,
    cachedTokens,
  };
  if (sawCached && inputTokens > 0) {
    // 两个百分比各自从原始和算，不用四舍五入过的中间值再乘，免得 47%×0.8 得 38
    summary.hitPct = Math.round((cachedTokens / inputTokens) * 100);
    summary.savedPct = Math.round((CACHE_SAVED_FACTOR * cachedTokens * 100) / inputTokens);
  }
  return { summary, missNotes };
}

/**
 * 给用户看的 token 数：千写 k、百万写 M（用户 2026-10-12 拍板要的单位）。
 *
 * - 不到 1000 照实写（"677"），不加千分位（"1,500" 在中文界面反而别扭）；
 * - 1k~999k 写 k：100k 以上取整（"300k"），以下是保留一位小数（"15.1k"、"8.9k"）；
 * - 100 万以上写 M：同样 100M 以上取整。
 * 三个地方（发送区状态条 / 设置页用量面板 / 上下文检查）共用这一个函数，口径只此一处。
 */
export function friendlyTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1_000) return String(Math.round(n));

  // 一律一位小数：参考图就是 148.2K/256.0K · 57.9%（几百 K 也照写 .0，别自作聪明取整）
  const one = (v: number) => v.toFixed(1);

  // 999,999 这种边界：按 K 算会四舍五入成 1000.0K，得改走 M，否则出现"1000.0K"这种怪数
  if (n < 999_500) return `${one(n / 1_000)}K`;
  return `${one(n / 1_000_000)}M`;
}
