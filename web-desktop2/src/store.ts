import { create } from "zustand";
import { loadPrefs, savePref } from "./adapters/prefsApi";
import type { PlanTask } from "./adapters/planStats";
import { clearLegacyTasks, loadTasks } from "./adapters/planRepo";
import {
  addPlanTasks as apiAddPlanTasks,
  deletePlanTask,
  fetchPlan,
  importPlan,
  planEvent,
  setPlanGoal as apiSetPlanGoal,
  resetPlan as apiResetPlan,
  setPlanPhases as apiSetPlanPhases,
  setPlanRhythm as apiSetPlanRhythm,
  setPlanDay as apiSetPlanDay,
  togglePlanTask,
  type PlanGoal,
} from "./features/plan/planApi";
import type { TrayItem } from "./adapters/batch";
import type { DetectionDecisionMap } from "./adapters/finalize";

export type View = "ask" | "dict" | "study" | "library" | "plan" | "home" | "reading";

// 外观（主题 / 密度 / 减少动效 / 消息字号）的逻辑都在 adapters/appearance.ts，
// 那边不碰 store 也不碰 localStorage，能脱离 DOM 测。这里只留读写那一层。
export {
  applyAppearance,
  parseAppearance,
  resolveTheme,
  systemPrefersDark,
  DEFAULT_APPEARANCE,
} from "./adapters/appearance";
export type {
  Appearance,
  Density,
  EnglishFont,
  LineHeight,
  LineWidth,
  MarkStyle,
  TextSize,
  ThemeMode,
} from "./adapters/appearance";
import {
  applyAppearance as apply,
  parseAppearance,
  type Appearance,
} from "./adapters/appearance";

/** 老键：主题曾经单独存一个裸字符串。留着做迁移兜底。 */
const THEME_KEY = "glyph.theme";
/** 新键：外观整个存成一个 JSON。 */
const APPEARANCE_KEY = "glyph.appearance";

function loadAppearance(): Appearance {
  try {
    return parseAppearance(
      localStorage.getItem(APPEARANCE_KEY),
      localStorage.getItem(THEME_KEY),
    );
  } catch {
    // localStorage 不可用（隐私模式 / 被禁）时走默认，不要因此炸掉整个应用
    return parseAppearance(null);
  }
}

interface Store {
  view: View;
  setView: (v: View) => void;
  sbHidden: boolean;
  toggleSb: () => void;
  panelOpen: boolean;
  setPanelOpen: (v: boolean) => void;
  appearance: Appearance;
  setAppearance: (p: Partial<Appearance>) => void;

  askDraft: string;
  askDraftAt: number;
  setAskDraft: (v: string) => void;

  tasks: PlanTask[];
  goal: PlanGoal | null;
  /** 阶段划分：第几周到第几周练什么 */
  phases: import("./features/plan/planApi").PlanPhase[];
  /** 周节奏：周一..周日各练什么（由后端每天展开成任务） */
  rhythm: import("./features/plan/planApi").PlanRhythm;
  planReady: boolean;
  planError: string | null;
  hydratePlan: () => Promise<void>;
  setPlanGoal: (text: string) => Promise<void>;
  /** 保存阶段划分（界面「调整」用） */
  savePhases: (phases: import("./features/plan/planApi").PlanPhase[]) => Promise<void>;
  /** 保存周节奏（界面「调整周节奏」用） */
  saveRhythm: (rhythm: import("./features/plan/planApi").PlanRhythm, sync?: boolean) => Promise<void>;
  /** 换掉某一天的任务；scope=weekly 同时写回周节奏 */
  savePlanDay: (
    date: string,
    items: { t: string }[],
    scope: "day" | "weekly",
  ) => Promise<void>;
  /** 清空整份计划（目标/阶段/节奏/任务） */
  resetPlan: () => Promise<void>;
  addTask: (t: string, scheduledFor?: string) => Promise<void>;
  /** 收下对话里的一整份计划：先存目标，再写任务 */
  acceptPlan: (
    goal: string,
    texts: string[],
    phases?: import("./features/plan/planApi").PlanPhase[],
    rhythm?: import("./features/plan/planApi").PlanRhythm,
    replace?: boolean,
  ) => Promise<void>;
  addPlanTasks: (texts: string[]) => Promise<void>;
  toggleTask: (id: string) => Promise<void>;
  removeTask: (id: string) => Promise<void>;
  /** 撤销删除：把刚删掉的任务原样塞回去（保留 id / 完成状态） */
  restoreTask: (task: import("./adapters/planStats").PlanTask) => Promise<void>;
  /** 背词 / 查词完成后回写计划；没有匹配任务时静默 */
  notifyPlanEvent: (kind: "study" | "dict", evidence?: string) => Promise<void>;

  /** 输入区待发送的附件（图片），上限由 batch 容量规则在组件层拦 */
  tray: TrayItem[];
  addTrayItem: (item: TrayItem) => void;
  removeTrayItem: (id: string) => void;
  clearTray: () => void;

  /** OCR 确认闸：itemId → 用户确认的转写稿（会话级，切会话清空） */
  confirmedOcr: Record<string, string>;
  confirmOcr: (id: string, text: string) => void;
  unconfirmOcr: (id: string) => void;
  clearConfirmedOcr: () => void;

  /** 批阅拍板：gradeCallId →（detectionId → 表态）。定稿后/切会话清空 */
  gradeDecisions: Record<string, DetectionDecisionMap>;
  setDetection: (callId: string, detId: string, decision: DetectionDecisionMap[string]) => void;
  /** 撤掉某一条之前的表态（回到待处理） */
  clearDetection: (callId: string, detId: string) => void;
  adoptAllDetections: (callId: string, detIds: string[]) => void;
  clearGradeDecisions: (callId?: string) => void;
  /** 台内复核结论：gradeCallId →（detectionId → verdict）；传 undefined = 撤销该条结论（恢复） */
  gradeRechecks: Record<string, Record<string, import("./adapters/inlineGrade").RecheckVerdictLike>>;
  setRecheckVerdict: (
    callId: string,
    detId: string,
    verdict: import("./adapters/inlineGrade").RecheckVerdictLike | undefined,
  ) => void;
  clearGradeRechecks: (callId?: string) => void;

  /** 放大并排画布：OCR 核对/批阅时刻全屏铺开（切会话/重置时关闭） */
  canvasMode: boolean;
  setCanvasMode: (v: boolean) => void;

  /** 本批复审配额：null=后端不可达/未知（不限制） */
  recheckQuota: import("./adapters/gradeApi").RecheckQuota | null;
  setRecheckQuota: (q: import("./adapters/gradeApi").RecheckQuota | null) => void;
}

/** dev 调试：?view=dict|study|library|plan|home|reading 直接进指定页（截图/走查用） */
const initialView = (): View => {
  if (!import.meta.env.DEV) return "ask";
  const v = new URLSearchParams(location.search).get("view");
  return v === "dict" || v === "study" || v === "plan" ? v : "ask";
};

export const useStore = create<Store>((set, get) => ({
  view: initialView(),
  setView: (v) => set({ view: v }),
  sbHidden: false,
  toggleSb: () => set(s => ({ sbHidden: !s.sbHidden })),
  panelOpen: false,
  setPanelOpen: (v) => set({ panelOpen: v }),
  appearance: loadAppearance(),
  setAppearance: (p) => {
    const appearance = { ...get().appearance, ...p };
    try {
      localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance));
      localStorage.setItem(THEME_KEY, appearance.mode);
    } catch { /* ignore */ }
    apply(appearance);
    savePref("appearance", appearance);
    set({ appearance });
  },

  askDraft: "",
  askDraftAt: 0,
  setAskDraft: (v) => set({ askDraft: v, askDraftAt: Date.now() }),

  tasks: [],
  goal: null,
  phases: [],
  rhythm: {},
  planReady: false,
  planError: null,
  hydratePlan: async () => {
    if (get().planReady) return;
    try {
      let snap = await fetchPlan();
      const legacy = loadTasks();
      if (snap.tasks.length === 0 && legacy.length > 0) {
        snap = await importPlan(
          snap.goal,
          legacy.map(t => ({
            id: t.id,
            t: t.t,
            src: t.src,
            done: t.done,
            createdAt: t.createdAt,
          })),
        );
        clearLegacyTasks();
      }
      set({
        tasks: snap.tasks,
        goal: snap.goal,
        phases: snap.phases,
        rhythm: snap.rhythm,
        planReady: true,
        planError: null,
      });
    } catch (error) {
      set({
        planReady: true,
        planError: error instanceof Error ? error.message : "计划读不出来，稍后再试。",
      });
    }
  },
  savePhases: async (phases) => {
    const snap = await apiSetPlanPhases(phases);
    set({ phases: snap.phases, rhythm: snap.rhythm, tasks: snap.tasks, planError: null });
  },
  resetPlan: async () => {
    const snap = await apiResetPlan();
    set({
      tasks: snap.tasks,
      goal: snap.goal,
      phases: snap.phases,
      rhythm: snap.rhythm,
      planReady: true,
      planError: null,
    });
  },
  savePlanDay: async (
    date: string,
    items: { t: string }[],
    scope: "day" | "weekly",
  ) => {
    const snap = await apiSetPlanDay(date, items, scope);
    set({ tasks: snap.tasks, goal: snap.goal, phases: snap.phases, rhythm: snap.rhythm, planError: null });
  },
  saveRhythm: async (rhythm, sync = false) => {
    const snap = await apiSetPlanRhythm(rhythm, sync);
    set({ phases: snap.phases, rhythm: snap.rhythm, tasks: snap.tasks, planError: null });
  },
  setPlanGoal: async (text) => {
    const snap = await apiSetPlanGoal(text);
    set({
      goal: snap.goal,
      tasks: snap.tasks,
      phases: snap.phases,
      rhythm: snap.rhythm,
      planReady: true,
      planError: null,
    });
  },
  addTask: async (text, scheduledFor) => {
    const snap = await apiAddPlanTasks([
      { t: text, kind: "custom", src: "own", scheduledFor: scheduledFor ?? null },
    ]);
    set({ tasks: snap.tasks, goal: snap.goal, phases: snap.phases, rhythm: snap.rhythm, planError: null });
  },
  addPlanTasks: async (texts) => {
    const snap = texts.length ? await apiAddPlanTasks(texts.map(t => ({ t }))) : await fetchPlan();
    set({ tasks: snap.tasks, goal: snap.goal, phases: snap.phases, rhythm: snap.rhythm, planError: null });
  },
  acceptPlan: async (goal, texts, phases = [], rhythm = {}, replace = false) => {
    if (replace) await apiResetPlan();
    await apiSetPlanGoal(goal);
    if (phases.length) await apiSetPlanPhases(phases);
    if (Object.keys(rhythm).length) await apiSetPlanRhythm(rhythm);
    // 只给了节奏、没有额外任务时，不要再写一次空任务（后端会回"没有有效任务"）
    const snap = texts.length
      ? await apiAddPlanTasks(texts.map(t => ({ t })))
      : await fetchPlan();
    set({
      tasks: snap.tasks,
      goal: snap.goal,
      phases: snap.phases,
      rhythm: snap.rhythm,
      planReady: true,
      planError: null,
    });
  },
  restoreTask: async (task) => {
    const snap = await apiAddPlanTasks([
      {
        id: task.id,
        t: task.t,
        kind: task.kind,
        src: task.src,
        target: task.target,
        progress: task.progress,
        done: task.done,
        doneAt: task.doneAtMs ?? undefined,
        scheduledFor: task.scheduledFor ?? undefined,
      },
    ]);
    set({ tasks: snap.tasks, goal: snap.goal, phases: snap.phases, rhythm: snap.rhythm, planError: null });
  },
  toggleTask: async (id) => {
    const before = get().tasks;
    const current = before.find(t => t.id === id);
    if (!current) return;
    const done = !current.done;
    set({
      tasks: before.map(t =>
        t.id === id
          ? { ...t, done, doneAt: done ? new Date().toISOString().slice(0, 10) : null }
          : t,
      ),
    });
    try {
      const snap = await togglePlanTask(id, done);
      set({ tasks: snap.tasks, goal: snap.goal, planError: null });
    } catch (error) {
      set({
        tasks: before,
        planError: error instanceof Error ? error.message : "计划没保存上，再试一次。",
      });
    }
  },
  removeTask: async (id) => {
    try {
      const snap = await deletePlanTask(id);
      set({ tasks: snap.tasks, goal: snap.goal, planError: null });
    } catch (error) {
      set({ planError: error instanceof Error ? error.message : "计划没删掉，再试一次。" });
    }
  },
  notifyPlanEvent: async (kind, evidence) => {
    try {
      const snap = await planEvent(kind, evidence);
      set({ tasks: snap.tasks, goal: snap.goal, planError: null });
    } catch {
      /* 自动完成失败不打扰用户；下一次学习还会再试 */
    }
  },

  tray: [],
  addTrayItem: (item) => set(s => ({ tray: [...s.tray, item] })),
  removeTrayItem: (id) => set(s => ({ tray: s.tray.filter(t => t.id !== id) })),
  clearTray: () => set({ tray: [] }),

  confirmedOcr: {},
  confirmOcr: (id, text) => set(s => ({ confirmedOcr: { ...s.confirmedOcr, [id]: text } })),
  unconfirmOcr: (id) => set(s => {
    const next = { ...s.confirmedOcr };
    delete next[id];
    return { confirmedOcr: next };
  }),
  clearConfirmedOcr: () => set({ confirmedOcr: {} }),

  gradeDecisions: {},
  setDetection: (callId, detId, decision) => set(s => ({
    gradeDecisions: {
      ...s.gradeDecisions,
      [callId]: { ...(s.gradeDecisions[callId] ?? {}), [detId]: decision },
    },
  })),
  adoptAllDetections: (callId, detIds) => set(s => {
    const cur = s.gradeDecisions[callId] ?? {};
    const merged: DetectionDecisionMap = { ...cur };
    for (const id of detIds) merged[id] = cur[id] ?? { action: "accepted" as const };
    return { gradeDecisions: { ...s.gradeDecisions, [callId]: merged } };
  }),
  clearDetection: (callId, detId) => set(s => {
    const cur = s.gradeDecisions[callId];
    if (!cur || !(detId in cur)) return {};
    const next = { ...cur };
    delete next[detId];
    return { gradeDecisions: { ...s.gradeDecisions, [callId]: next } };
  }),
  clearGradeDecisions: (callId) => set(s => {
    if (!callId) return { gradeDecisions: {} };
    const next = { ...s.gradeDecisions };
    delete next[callId];
    return { gradeDecisions: next };
  }),

  gradeRechecks: {},
  setRecheckVerdict: (callId, detId, verdict) => set(s => {
    const cur = s.gradeRechecks[callId] ?? {};
    const nextMap = { ...cur };
    if (verdict === undefined) delete nextMap[detId];
    else nextMap[detId] = verdict;
    return { gradeRechecks: { ...s.gradeRechecks, [callId]: nextMap } };
  }),
  clearGradeRechecks: (callId) => set(s => {
    if (!callId) return { gradeRechecks: {} };
    const next = { ...s.gradeRechecks };
    delete next[callId];
    return { gradeRechecks: next };
  }),

  canvasMode: false,
  setCanvasMode: (v) => set({ canvasMode: v }),

  recheckQuota: null,
  setRecheckQuota: (q) => set({ recheckQuota: q }),
}));

// 启动即应用一次，避免首屏闪烁；auto 模式下跟随系统深浅实时切换
apply(useStore.getState().appearance);
if (typeof window !== "undefined" && window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (useStore.getState().appearance.mode === "auto") apply(useStore.getState().appearance);
  };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else mq.addListener(onChange);
}


/* 启动时从后端补读设置：桌面端端口每次不同，localStorage 按网址隔离，
 * 只靠本地存储会把用户的主题/字号重置。后端读到了就以它为准。 */
export async function hydrateAppearanceFromServer(): Promise<void> {
  const prefs = await loadPrefs();
  const saved = prefs["appearance"];
  if (!saved || typeof saved !== "object") return;
  try {
    const next = parseAppearance(JSON.stringify(saved), null);
    const current = useStore.getState().appearance;
    if (JSON.stringify(next) === JSON.stringify(current)) return;
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(next));
    localStorage.setItem(THEME_KEY, next.mode);
    apply(next);
    useStore.setState({ appearance: next });
  } catch { /* 坏数据当没存过 */ }
}
