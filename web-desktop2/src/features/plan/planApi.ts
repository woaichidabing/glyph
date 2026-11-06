/** 计划后端接口：目标 / 任务 / 自动完成 / 旧数据迁移。 */
import type { PlanTask } from "../../adapters/planStats";

export type PlanGoal = {
  text: string;
  weeks: number;
  startDate: string;
  createdAt: number;
};

type ApiTask = {
  id: string;
  t: string;
  src: "glyph" | "own";
  kind?: "study" | "dict" | "custom";
  target?: number;
  progress?: number;
  done: boolean;
  createdAt: string;
  scheduledFor?: string | null;
  doneAt?: number | null;
  evidence?: string | null;
};

/** 阶段：第 fromWeek–toWeek 周练什么 */
export type PlanPhase = {
  fromWeek: number;
  toWeek: number;
  title: string;
  focus?: string;
};

/** 周节奏：键 "1".."7"（周一=1），值是当天固定要练的事 */
export type PlanRhythm = Record<string, { t: string; kind?: PlanTask["kind"] }[]>;

export type PlanSnapshot = {
  version: number;
  goal: PlanGoal | null;
  phases: PlanPhase[];
  rhythm: PlanRhythm;
  tasks: PlanTask[];
};

type RawSnapshot = {
  version: number;
  goal: PlanGoal | null;
  phases?: PlanPhase[];
  rhythm?: PlanRhythm;
  tasks: ApiTask[];
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = (await res.json()) as { detail?: string };
      detail = body.detail ?? "";
    } catch {
      /* ignore */
    }
    throw new Error(detail || "计划接口暂时不可用");
  }
  return (await res.json()) as T;
}

function dateOf(ms: number | null | undefined): string | null {
  if (!ms) return null;
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}

function toTask(raw: ApiTask): PlanTask {
  return {
    id: raw.id,
    t: raw.t,
    src: raw.src,
    done: raw.done,
    createdAt: raw.createdAt,
    doneAt: dateOf(raw.doneAt),
    doneAtMs: raw.doneAt ?? null,
    kind: raw.kind ?? "custom",
    target: raw.target ?? 1,
    progress: raw.progress ?? 0,
    scheduledFor: raw.scheduledFor ?? null,
    evidence: raw.evidence ?? null,
  };
}

function toSnapshot(raw: RawSnapshot): PlanSnapshot {
  return {
    version: raw.version,
    goal: raw.goal,
    phases: raw.phases ?? [],
    rhythm: raw.rhythm ?? {},
    tasks: raw.tasks.map(toTask),
  };
}

/** 清空整份计划（换计划时用） */
export async function resetPlan(): Promise<PlanSnapshot> {
  return toSnapshot(await request<RawSnapshot>("/api/plan/reset", { method: "POST" }));
}

export async function setPlanPhases(phases: PlanPhase[]): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot>("/api/plan/phases", {
      method: "PUT",
      body: JSON.stringify({ phases }),
    }),
  );
}

export async function setPlanRhythm(rhythm: PlanRhythm, sync = false): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot>("/api/plan/rhythm", {
      method: "PUT",
      body: JSON.stringify({ rhythm, sync }),
    }),
  );
}

/** 换掉某一天的任务；scope=weekly 时同时把这一天的条目写回周节奏。 */
export async function setPlanDay(
  date: string,
  items: { t: string }[],
  scope: "day" | "weekly",
): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot>("/api/plan/day", {
      method: "POST",
      body: JSON.stringify({ date, items, scope }),
    }),
  );
}

export async function fetchPlan(): Promise<PlanSnapshot> {
  return toSnapshot(await request<RawSnapshot>("/api/plan"));
}

export async function setPlanGoal(text: string, weeks = 12): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot>("/api/plan/goal", {
      method: "POST",
      body: JSON.stringify({ text, weeks }),
    }),
  );
}

export async function addPlanTasks(
  tasks: {
    t: string;
    /** 撤销删除时用：带上原来的 id 与完成状态，塞回去还是同一条 */
    id?: string;
    kind?: PlanTask["kind"];
    src?: "own" | "glyph";
    target?: number;
    progress?: number;
    done?: boolean;
    doneAt?: number | null;
    scheduledFor?: string | null;
  }[],
): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot>("/api/plan/tasks", {
      method: "POST",
      body: JSON.stringify({ tasks }),
    }),
  );
}

export async function togglePlanTask(id: string, done: boolean): Promise<PlanSnapshot> {
  const raw = await request<RawSnapshot & { task: ApiTask }>(
    "/api/plan/tasks/" + encodeURIComponent(id),
    { method: "PATCH", body: JSON.stringify({ done }) },
  );
  return toSnapshot(raw);
}

export async function deletePlanTask(id: string): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot & { removed: ApiTask }>(
      "/api/plan/tasks/" + encodeURIComponent(id),
      { method: "DELETE" },
    ),
  );
}

export async function planEvent(
  kind: "study" | "dict",
  evidence?: string,
  amount = 1,
): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot & { task: ApiTask | null }>("/api/plan/event", {
      method: "POST",
      body: JSON.stringify({ kind, evidence, amount }),
    }),
  );
}

export async function importPlan(
  goal: PlanGoal | null,
  tasks: { id: string; t: string; src: "glyph" | "own"; done: boolean; createdAt?: string }[],
): Promise<PlanSnapshot> {
  return toSnapshot(
    await request<RawSnapshot & { importedTasks: number }>("/api/plan/import", {
      method: "POST",
      body: JSON.stringify({ goal, tasks }),
    }),
  );
}
