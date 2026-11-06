import { create } from "zustand";
import { hydrateStudy, scheduleStudySync } from "./studySync";
import { setStudyChangeListener } from "./studyRepo";
import type { DailyQueue, ErrorWord, WordCardState } from "../../types";
import * as repo from "./studyRepo";
import type { Grade } from "../../adapters/srs";

type State = {
  settings: repo.Settings;
  queue: DailyQueue;
  hydrateRemote: () => Promise<boolean>;
  cards: Record<string, WordCardState>;
  errors: ErrorWord[];

  refresh: () => void;
  patchSettings: (p: Partial<repo.Settings>) => void;
  rebuild: () => void;
  grade: (
    word: string,
    g: Grade,
    kind: "new" | "review" | "wrong",
    answer?: { ok: boolean; ms: number; streak?: number; known?: boolean },
  ) => void;
  /** 只记连对次数，不动档位（一个词要连对三次才算出师，中间那两次不算数） */
  markStreak: (
    word: string,
    streak: number,
    known?: boolean,
    tally?: { ok: boolean; ms: number },
  ) => void;
  dropError: (word: string) => void;
  /** 清空错词本（不影响背词进度） */
  clearErrors: () => void;
  restoreError: (item: ErrorWord) => void;
};

export const useStudy = create<State>((set, get) => ({
  settings: repo.getSettings(),
  queue: repo.buildQueue(),
  hydrateRemote: async () => {
    const restored = await hydrateStudy();
    if (restored) get().refresh();
    return restored;
  },
  cards: repo.getCards(),
  errors: repo.getErrors(),

  refresh: () =>
    set({
      settings: repo.getSettings(),
      queue: repo.buildQueue(),
      cards: repo.getCards(),
      errors: repo.getErrors(),
    }),

  patchSettings: (p) => {
    const settings = repo.setSettings(p);
    // 只改设置不动今天已经背过的队列；新额度从明天生效。
    set({ settings, queue: repo.buildQueue() });
  },

  rebuild: () => set({ queue: repo.buildQueue(true) }),

  grade: (word, g, kind, answer) => {
    repo.recordGrade(word, g, kind, answer);
    get().refresh();
  },

  markStreak: (word, streak, known, tally) => {
    repo.setStreak(word, streak, known, tally);
    get().refresh();
  },

  dropError: (word) => {
    repo.removeError(word);
    get().refresh();
  },

  clearErrors: () => {
    repo.clearErrors();
    get().refresh();
  },

  restoreError: (item) => {
    repo.restoreError(item);
    get().refresh();
  },
}));

setStudyChangeListener(scheduleStudySync);
