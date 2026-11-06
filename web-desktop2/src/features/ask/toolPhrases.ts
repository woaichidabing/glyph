/**
 * 工具在对话时间线上的**中文说法**（纯函数：无 React、无 DOM、无网络）。
 *
 * 为什么单独成文件、且必须是纯函数：
 * 组头那行字是**前端查表拼出来的**，不是模型说的。**一旦让模型写这行字，它就能借这句
 * 「发言」**——冒充模型发言在批改场景尤其危险：用户会以为 AI 承诺了什么，而它其实只是
 * 在跑工具。抽成纯函数还换来一件事：这些措辞能在离线测试里被逐条钉死，回归不靠肉眼。
 * （本文件是工具中文说法的**唯一单点**；早期并存的那张 `components/toolMeta.ts` 表已删除。）
 *
 * 措辞与规则的单点：`docs/design/ask-transcript-render-design.md` §3。
 */

import type { ToolState } from "../../adapters/askTimeline";
import { FAIL_FALLBACK } from "./toolErrorText";

/** 一个工具的中文说法三件套 */
type ToolPhrase = {
  /** 展开态逐项里的动作，如「批改英文」（不带「正在」，组头会统一加） */
  action: string;
  /** 单次调用的过去式，如「批改了英文」 */
  past: string;
  /** 带数量的形态，`{n}` 处填数字；缺省表示这个工具不该报数量 */
  counted?: string;
  /** 数量达到几才用 `counted` 形态；缺省 2（联网搜索的来源数单次也要说，故为 1） */
  countedFrom?: number;
};

const TABLE: Record<string, ToolPhrase> = {
  grade_text: { action: "批改英文", past: "批改了英文", counted: "批改了 {n} 篇英文" },
  ocr_image: { action: "认图片文字", past: "认了图片文字", counted: "认了 {n} 张图的文字" },
  describe_image: { action: "看图", past: "看了图", counted: "看了 {n} 张图" },
  // 联网搜索的「数量」是**来源个数**，不是调用次数：一次搜索也要说清搜到几个，故从 1 起算
  web_search: {
    action: "联网搜索",
    past: "联网搜索了 1 个来源",
    counted: "联网搜索了 {n} 个来源",
    countedFrom: 1,
  },
  lookup_word: { action: "查词典", past: "查了词典", counted: "查了 {n} 个词" },
  recheck_detection: { action: "复核", past: "复核了一条", counted: "复核了 {n} 条" },
  read_file: { action: "读材料", past: "读了材料", counted: "读了 {n} 份材料" },
  read_memory: { action: "翻长期记忆", past: "翻了长期记忆" },
  read_artifact: { action: "调阅旧批改", past: "调阅了旧批改" },
  analyze_writing: { action: "评写作水平", past: "评了写作水平" },
  polish_text: { action: "做表达升档", past: "做了表达升档" },
  get_quiz: { action: "出题", past: "出了题" },
  check_quiz_answer: { action: "核对答案", past: "核对了答案" },
  propose_plan: { action: "排学习计划", past: "排了学习计划" },
  // 收词卡：正常情况卡片会收掉这一行；只有未收录 / 已收藏 / 失败时才退回工具行
  propose_wordbook_add: { action: "递收词卡", past: "递了收词卡" },
  remember: { action: "记", past: "记了一条" },
  forget: { action: "忘", past: "忘了一条" },
  record_review_result: { action: "记复习结果", past: "记了复习结果" },
  record_attempt: { action: "存练习记录", past: "存了练习记录" },
  read_attempts: { action: "翻练习记录", past: "翻了练习记录" },
  delete_attempt: { action: "删练习记录", past: "删了一条练习记录" },
};

/** 展开态逐项里的中文动作。不认识的工具退回工具名——未知工具也要说得出话。 */
export function actionPhrase(name: string): string {
  return TABLE[name]?.action ?? name;
}

/** 组头里的过去式短语。`count` 是该工具自己的计数口径（批改=篇数、搜索=来源数）。 */
export function pastPhrase(name: string, count = 1): string {
  const entry = TABLE[name];
  if (!entry) return name;
  const from = entry.countedFrom ?? 2;
  if (entry.counted && count >= from) return entry.counted.replace("{n}", String(count));
  return entry.past;
}

/** 整组工具的组头形态：失败、被停、进行中、完成 */
export type GroupPhase = "running" | "done" | "failed" | "cancelled";

/** 组头里的一项。同名多项会被合并计数。 */
export type GroupItem = {
  name: string;
  state: ToolState;
  /** 计数口径（批改=篇数、搜索=来源数）；缺省算 1 次 */
  count?: number;
  /** 失败时接在动作后面的人话后缀，如「没连上」（来自 toolErrorText） */
  failReason?: string;
  /** 这一步的开始时刻 / 耗时（后端给的）。算整组耗时用，缺了就不显示。 */
  startedAt?: number;
  durationMs?: number;
};

export type GroupTitle = {
  phase: GroupPhase;
  text: string;
};

/**
 * 这一组从第一步开始到最后一步结束花了多久（毫秒）。
 *
 * 用各步的 `startedAt + durationMs` **精确算，不靠前端掐表** —— 界面重挂载、
 * 切换会话都不会飘。任何一步缺耗时（还在跑 / 后端没回）就返回 null，
 * 调用方据此**整条不显示**，绝不猜一个数出来。
 *
 * 并行重叠的步按「最早开始 → 最晚结束」算，不是把耗时加起来。
 */
export function groupElapsedMs(items: readonly GroupItem[]): number | null {
  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  for (const it of items) {
    if (it.startedAt === undefined || it.durationMs === undefined) return null;
    min = Math.min(min, it.startedAt);
    max = Math.max(max, it.startedAt + it.durationMs);
  }
  return Number.isFinite(min) && max >= min ? max - min : null;
}

/** 毫秒说成人话：`3s` / `1m 3s`。负数和小数都不炸。 */
export function elapsedLabel(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${total % 60}s`;
}

/**
 * 拼一整组的组头。空组返回 `null`（调用方据此不渲染组头）。
 *
 * 优先级：被停 > 进行中 > 完成 / 失败——「我停的」不能读成「坏了」（设计稿 §7.2）。
 */
export function buildToolGroupTitle(items: GroupItem[]): GroupTitle | null {
  if (items.length === 0) return null;

  if (items.some((it) => it.state === "cancelled")) {
    return { phase: "cancelled", text: "已停止" };
  }

  // 同类合并、按首次出现顺序：并行工具谁先跑完谁先落盘，直接按结果顺序会串位
  const order: string[] = [];
  const merged = new Map<string, { count: number; state: ToolState; failReason?: string }>();
  for (const it of items) {
    let slot = merged.get(it.name);
    if (!slot) {
      slot = { count: 0, state: it.state };
      merged.set(it.name, slot);
      order.push(it.name);
    }
    slot.count += it.count ?? 1;
    if (it.state === "fail") {
      slot.state = "fail";
      slot.failReason = it.failReason ?? slot.failReason;
    }
  }

  if (items.some((it) => it.state === "running")) {
    // 长工具（批改/转写常要几十秒）要把"已经等了多久"说出来，否则界面看起来像卡死。
    // 只给已跑起来且拿到 startedAt 的步算；缺时间戳就不编一个数。
    const running = items.filter((it) => it.state === "running" && it.startedAt !== undefined);
    const waited =
      running.length > 0 ? Math.max(0, Date.now() - Math.min(...running.map((it) => it.startedAt!))) : 0;
    const tail = waited >= 3000 ? ` · 已等 ${elapsedLabel(waited)}` : "";
    return { phase: "running", text: `正在${order.map(actionPhrase).join("、")}…${tail}` };
  }

  // 整组耗时只加在**跑完的**组头上：还在跑的时候算出来是个半截数，不如不显示
  const elapsed = groupElapsedMs(items);
  const tail = elapsed === null ? "" : ` · 共 ${elapsedLabel(elapsed)}`;

  const failed = order.filter((n) => merged.get(n)!.state === "fail");
  if (failed.length === 0) {
    return {
      phase: "done",
      text: order.map((n) => pastPhrase(n, merged.get(n)!.count)).join("，"),
    };
  }

  const failPart = (names: string[]) =>
    names.map((n) => `${actionPhrase(n)}${merged.get(n)!.failReason ?? FAIL_FALLBACK}`).join("、");

  if (failed.length === order.length) {
    // 全失败：没有可报的成功项，逐条说清哪一步没成（顿号连，最后一项也带原因）
    return {
      phase: "failed",
      text: order.map((n) => `${actionPhrase(n)}${merged.get(n)!.failReason ?? FAIL_FALLBACK}`).join("，"),
    };
  }

  const okPart = order
    .filter((n) => merged.get(n)!.state !== "fail")
    .map((n) => pastPhrase(n, merged.get(n)!.count))
    .join("，");
  return { phase: "failed", text: `${okPart}，但${failPart(failed)}` };
}

/** 关键参数的最长显示长度（含省略号）。超了就把整行挤爆，不如截断。 */
const PARAM_MAX = 48;

/** 每个工具的「关键参数」从哪个入参字段取。表里没有的工具就不显示参数 chip。 */
const PARAM_FIELD: Record<string, string> = {
  web_search: "query",
  lookup_word: "word",
  read_file: "path",
};

function clip(value: string): string {
  return value.length <= PARAM_MAX ? value : `${value.slice(0, PARAM_MAX - 1)}…`;
}

/**
 * 展开态逐项里显示的**关键参数**，让用户能核对「它是不是在查我说的那个词」。
 *
 * 三条边界：
 * - 表里没有的工具、字段缺失、值空白 → 返回 `undefined`（调用方整个不渲染 chip，
 *   不显示空壳，更不许编一个）；
 * - 路径只留文件名：用户关心的是「哪一份」，不关心它躺在哪一级目录；
 * - 过长截断，不把一行挤爆。
 */
export function toolParam(name: string, args?: Record<string, unknown>): string | undefined {
  const field = PARAM_FIELD[name];
  if (!field || !args) return undefined;

  const raw = args[field];
  if (typeof raw !== "string") return undefined;
  const value = raw.trim();
  if (!value) return undefined;

  const shown = field === "path" ? value.split(/[\\/]/).pop() || value : value;
  return clip(shown);
}