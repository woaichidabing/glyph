import { readJSON, writeJSON } from "./localStore";
import { todayStr } from "./streakRepo";

/**
 * 已拿下错词：从错词本里被移出的那些词。
 *
 * 为什么要把"移出"留成一份账：
 *   错词本移出之后就再没有这个词的痕迹了，可"这个词我以前总错、现在会了"
 *   恰恰是学习里唯一能证明真进步的证据。删掉它就等于把功劳簿撕了。
 */
export type Mastered = { word: string; date: string; from?: string };

const KEY = "agent.mastered.v1";

export function readMastered(): Mastered[] {
  return readJSON<Mastered[]>(KEY, []);
}

/** 幂等：同一个词只记一次 */
export function addMastered(word: string, from?: string): Mastered[] {
  const all = readMastered();
  if (all.some((m) => m.word === word)) return all;
  writeJSON(KEY, [...all, { word, date: todayStr(), from }]);
  return readMastered();
}

export function clearMastered(): void {
  writeJSON(KEY, []);
}
