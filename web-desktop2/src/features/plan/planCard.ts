/* ============================================================
 * propose_plan 工具结果 → 计划提议卡数据（纯解析，不碰存储 / DOM）。
 * 落库在组件里走 store.addPlanTasks（一次写入、src=glyph）。
 * ============================================================ */

export type PlanProposal = {
  /** 一句话目标（仅卡片展示；PlanTask 暂无 goal 容器，不入库） */
  goal: string;
  /** 逐条可执行任务文本，已 trim、去空 */
  tasks: string[];
  /** 阶段划分（可选）：第几周到第几周练什么 */
  phases: { fromWeek: number; toWeek: number; title: string; focus?: string }[];
  /** 周节奏（可选）：键 "1".."7"（周一=1） */
  rhythm: Record<string, { t: string; kind?: "study" | "dict" | "custom" }[]>;
};

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

/**
 * 从 propose_plan 的 tool_result.output 解析计划。
 * 任何字段缺失 / 全空都返回 null（调用方据此不渲染卡片），绝不抛错。
 */
export function extractPlan(output: unknown): PlanProposal | null {
  if (!isRecord(output)) return null;
  if (output.proposed !== true) return null;

  const goal = typeof output.goal === "string" ? output.goal.trim() : "";
  if (!goal) return null;

  const rawTasks = Array.isArray(output.tasks) ? output.tasks : [];
  const tasks: string[] = [];
  for (const item of rawTasks) {
    const text = isRecord(item) ? item.t : item;
    if (typeof text === "string") {
      const clean = text.trim();
      if (clean) tasks.push(clean);
    }
  }
  if (tasks.length === 0) return null;

  // 阶段：结构不对就整条丢，不猜
  const phases: PlanProposal["phases"] = [];
  if (Array.isArray(output.phases)) {
    for (const item of output.phases) {
      if (!isRecord(item)) continue;
      const from = Number(item.fromWeek);
      const to = Number(item.toWeek);
      const title = typeof item.title === "string" ? item.title.trim() : "";
      if (!Number.isInteger(from) || !Number.isInteger(to) || from < 1 || to < from || !title) continue;
      phases.push({
        fromWeek: from,
        toWeek: to,
        title,
        focus: typeof item.focus === "string" ? item.focus.trim() : undefined,
      });
    }
  }

  // 周节奏：只认 1..7，任务文案非空
  const rhythm: PlanProposal["rhythm"] = {};
  if (isRecord(output.rhythm)) {
    for (const key of ["1", "2", "3", "4", "5", "6", "7"]) {
      const list = output.rhythm[key];
      if (!Array.isArray(list)) continue;
      const items = list
        .map((x) => {
          const text = isRecord(x) ? x.t : x;
          return typeof text === "string" && text.trim() ? { t: text.trim() } : null;
        })
        .filter((x): x is { t: string } => x !== null);
      if (items.length) rhythm[key] = items;
    }
  }

  return { goal, tasks, phases, rhythm };
}
