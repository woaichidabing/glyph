/**
 * ask_user「询问用户」卡片的纯逻辑：工具参数解析 + 工具结果收口。
 *
 * 契约逐字对齐后端 src/glyph/tools/ask_user.py：
 * - 入参 question + options[{label,description}]，选项 2~4 个；
 * - 推荐项放第一个，label 尾缀 " (Recommended)"，界面渲染成「推荐」徽章；
 * - 结果三档：
 *     steer     参数不合规，工具秒回一句教模型改的话——这一问**根本不该露面**，卡片收掉；
 *     answered  用户答了，choice=选项原文 或 custom_text=用户自己打的话（text 优先）；
 *     no_answer 没等到回答——同样收卡，不冒充用户选了什么。
 *
 * 死卡防护是这里的头号职责：卡片一旦出现，后续任何结果/终态/取消都必须让它离开
 * waiting，绝不允许界面留下一张永远转圈、点什么都没反应的卡。
 */

export type AskOption = {
  label: string;
  description: string;
  /** 后端把推荐项放第一个并在 label 尾缀 (Recommended) */
  rec: boolean;
};

export type AskCardState = "waiting" | "answered" | "dismissed";

export type AskNode = {
  id: string;
  kind: "ask";
  /** 与 SSE tool_call / tool_result 的 call.id 配对，不能用序号 */
  callId: string;
  question: string;
  options: AskOption[];
  state: AskCardState;
  /** 答案已提交、正在等 SSE 配对结果：按钮禁用防重复点击 */
  submitting?: boolean;
  /** answered：用户的答案摘要（经后端对账后的 choice/custom_text） */
  answer?: string;
  answerKind?: "choice" | "custom";
  sortKey: number;
};

/** 提交给 /api/ask-user/answer 的载荷；custom 与 choice 同源时只发 text */
export type AskSubmission =
  | { kind: "choice"; choice: string; question: string }
  | { kind: "custom"; text: string; question: string };

/**
 * 构造提交载荷。用户自己打了字（非空白）就按 custom 提交——他很可能是在改需求；
 * 否则交点选的 choice；两者都空返回 null（不发没有信息量的空请求）。
 */
export function buildSubmission(
  question: string,
  choice: string | null | undefined,
  text: string,
): AskSubmission | null {
  const note = text.trim();
  if (note) return { kind: "custom", text: note, question };
  const picked = (choice ?? "").trim();
  if (picked) return { kind: "choice", choice: picked, question };
  return null;
}

/** 受理一次提交：waiting → submitting（禁用按钮）；非等待中原样返回（防重复/防误触） */
export function markSubmitting(node: AskNode): AskNode {
  if (node.state !== "waiting" || node.submitting) return node;
  return { ...node, submitting: true };
}

/**
 * 提交请求落地：后端回 ok=false 是迟到点击（问题已取消/对不上），静默收卡；
 * ok=true 只是「答案已送达」，卡片保持 submitting，等同一条流上的 tool_result
 * 把它正式收成 answered——不能在请求成功时就假装模型已经答完。
 */
export function settleSubmitted(node: AskNode, ok: boolean): AskNode {
  // 已被 tool_result / 终态收口的卡片不许再翻案
  if (node.state !== "waiting") return node;
  if (ok) return node;
  return { ...node, state: "dismissed", submitting: false };
}

/** 网络层失败（没送到后端）：退回可重试的 waiting，别把用户的选择吞掉 */
export function resetSubmitting(node: AskNode): AskNode {
  if (node.state !== "waiting") return node;
  return { ...node, submitting: false };
}

export type ParsedAsk = { question: string; options: AskOption[] };

const MIN_OPTIONS = 2;
const MAX_OPTIONS = 4;
/** 后端契约的推荐尾缀（英文、半角括号）；中文内容里的"推荐"二字不由此推断 */
const REC_SUFFIX = "(Recommended)";

/**
 * 解析并校验 ask_user 的工具参数。
 * 任何不合规（问题空、选项数量不对、标签缺失）一律返回 null——
 * 前端不替模型补选项、不猜问题；这种调用后端会秒回 steer，由结果收掉占位卡。
 */
export function parseAskArgs(
  args: Record<string, unknown> | undefined,
): ParsedAsk | null {
  if (!args) return null;
  const question = typeof args.question === "string" ? args.question.trim() : "";
  if (!question) return null;

  const raw = args.options;
  if (!Array.isArray(raw)) return null;
  if (raw.length < MIN_OPTIONS || raw.length > MAX_OPTIONS) return null;

  const options: AskOption[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") return null;
    const o = item as Record<string, unknown>;
    if (typeof o.label !== "string" || !o.label.trim()) return null;
    let label = o.label.trim();
    let rec = false;
    if (label.endsWith(REC_SUFFIX)) {
      rec = true;
      label = label.slice(0, -REC_SUFFIX.length).trim();
    }
    const description =
      typeof o.description === "string" ? o.description.trim() : "";
    options.push({ label, description, rec });
  }
  return { question, options };
}

export type AskResolution =
  | { state: "answered"; answer: string; kind: "choice" | "custom" }
  | { state: "dismissed" };

/**
 * 工具结果决定卡片去向。
 * steer / no_answer / 工具失败 / 形态不认识——全部 dismissed；
 * 只有带合法答案的 answered 才转已答。custom_text 与 choice 同时存在时以 custom_text 为准
 * （后端契约：用户自己打了字，就按他说明的办）。
 */
export function resolveAskResult(output: unknown, ok: boolean): AskResolution {
  const o =
    output && typeof output === "object"
      ? (output as Record<string, unknown>)
      : undefined;
  const status = o && typeof o.status === "string" ? o.status : "";
  if (status === "answered") {
    const custom =
      typeof o?.custom_text === "string" ? o.custom_text.trim() : "";
    if (custom) return { state: "answered", answer: custom, kind: "custom" };
    const choice = typeof o?.choice === "string" ? o.choice.trim() : "";
    if (choice) return { state: "answered", answer: choice, kind: "choice" };
    return { state: "dismissed" }; // 声称 answered 却没有任何答案：形态坏了，收卡
  }
  // ok 仅作记录：任何非 answered 结果都不允许卡片留在 waiting
  void ok;
  return { state: "dismissed" };
}
