import { MINI, MINI_WORDS } from "../../adapters/miniDict";
import type { Sense } from "../../types";

/**
 * 学习卡的四个选项从哪儿来（方案 5.6.3 · 选择题式学习）
 *
 * 为什么不是"随便抓三个词"：
 *   干扰项太好认，答题就退化成"点第一个像的"，反应时间也就没了意义——
 *   而自动定档恰恰是靠反应时间在分辨"脱口而出"和"盯着想了十秒"。
 *   所以干扰项要挑**真的会混的**：同词性、形近、或干脆就是它的近义反义。
 *
 * 排序的依据（分数越高越像）：
 *   +4  它的近义词 / 反义词 —— 义近最难分，是理想的干扰项
 *   +3  同一词性 —— "v. 影响" 与 "v. 测量" 摆在人是分得出高下的
 *   +3  编辑距离 ≤ 2（如 complement / compliment）
 *   +4  共同前缀 ≥ 4 个字母（如 principle / principal）
 *   +2  随机的零头，免得每次背到同一个词都撞上同一组干扰项
 */

export type Choice = {
  /** 这个选项是哪个词 */
  word: string;
  /** 词性：小字淡色标在选项左上角 */
  pos: string;
  /** 主行：看词选义是中文释义，听音选词是英文单词本身 */
  text: string;
  /** 次行：听音选词里补一行中文，帮人确认没听岔 */
  sub?: string;
  right: boolean;
};

export type Quiz = {
  word: string;
  /** 正确那一项的完整释义与例句（作答后要摊开给人看） */
  sense: Sense;
  phonetic: { uk: string; us: string };
  /** 已打乱，第一个不一定是答案 */
  choices: Choice[];
};

/** 只取第一个分句：四个选项长短差太多，人一眼就看出"最长的那个是答案" */
function firstClause(s: string): string {
  return s.split(/[；;]/)[0].trim();
}

function editDistance(a: string, b: string): number {
  const m = a.length;
  const n = b.length;
  if (m === 0) return n;
  if (n === 0) return m;
  let prev = Array.from({ length: n + 1 }, (_, i) => i);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) {
      cur[j] = Math.min(
        prev[j] + 1,
        cur[j - 1] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      );
    }
    prev = cur;
  }
  return prev[n];
}

/** 形近度：共同前缀 + 编辑距离 */
function shapeNear(a: string, b: string): number {
  const p = a.toLowerCase();
  const q = b.toLowerCase();
  let same = 0;
  while (same < Math.min(p.length, q.length) && p[same] === q[same]) same += 1;
  const dist = editDistance(p, q);
  let n = 0;
  if (same >= 4) n += 4;
  else if (same >= 3) n += 3;
  else if (same >= 2) n += 1;
  if (dist <= 2) n += 3;
  else if (dist <= 3) n += 1;
  return n;
}

function shuffle<T>(arr: T[]): T[] {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** 词书页带来的轻量词条（wordbooks.bookGloss 的产物） */
export type QuizPoolEntry = {
  word: string;
  phonetic?: string;
  pos?: string | null;
  gloss: string;
};

type GlossLike = {
  word: string;
  phonetic: { uk: string; us: string };
  senses: Sense[];
  synonyms?: string[];
  antonyms?: string[];
};

function poolEntryToGloss(e: QuizPoolEntry): GlossLike {
  const phon = e.phonetic ?? "";
  return {
    word: e.word,
    phonetic: { uk: phon, us: phon },
    senses: [{ pos: e.pos ?? "", translation: e.gloss, examples: [] }],
  };
}

/**
 * 出一道题。目标词没有本地释义时返回 null——
 * 那种词没法出选择题（干扰项无从谈起），交给调用方退化成"认不认识"两问。
 *
 * pool 是后端词书页的轻量词条（真词书场景，几百个同书词）：
 * 目标词与干扰项都优先从池里出；池不够三个干扰项时用内置 mini 库补齐。
 */
export function buildQuiz(word: string, pool?: QuizPoolEntry[]): Quiz | null {
  const key = word.toLowerCase();
  const poolMe = pool?.find((e) => e.word.toLowerCase() === key && e.gloss);
  const me: GlossLike | undefined = poolMe ? poolEntryToGloss(poolMe) : MINI[key];
  if (!me || !me.senses.length) return null;

  // 词书池先占位（同词以池为准），mini 库补在后面
  const poolMap = new Map<string, GlossLike>();
  for (const e of pool ?? []) {
    const k = e.word.toLowerCase();
    if (k !== key && e.gloss && !poolMap.has(k)) poolMap.set(k, poolEntryToGloss(e));
  }

  const sense = me.senses[0];
  const targetPos = sense.pos.replace(/\s*\/\s*.*$/, "").trim(); // "n. / v." → "n."
  const related = new Set(
    [...(me.synonyms ?? []), ...(me.antonyms ?? [])].map((w) => w.toLowerCase().split(/\s+/)[0]),
  );

  const targetText = firstClause(sense.translation);

  type Cand = { w: string; pos: string; text: string; n: number };
  const score = (w: string, e: GlossLike): Cand | null => {
    const s = e.senses[0];
    const text = firstClause(s.translation);
    // 释义撞车就作废：两个一模一样的选项，人只能瞎点，用时也就没了意义
    if (!text || text === targetText) return null;
    const pos = s.pos.replace(/\s*\/\s*.*$/, "").trim();
    const lk = w.toLowerCase();
    let n = Math.random() * 2;
    if (pos === targetPos) n += 3;
    if (related.has(lk)) n += 4;
    n += shapeNear(word, w);
    return { w: e.word, pos, text, n };
  };

  // 词书池候选够三个就只用同书词（难度一致）；不够才拿 mini 库补
  const poolCands: Cand[] = [];
  for (const [lk, e] of poolMap) {
    const c = score(e.word, e);
    if (c) poolCands.push(c);
  }
  let cands = poolCands;
  if (poolCands.length < 3) {
    const extra: Cand[] = [];
    const used = new Set(poolCands.map((c) => c.w.toLowerCase()));
    for (const w of MINI_WORDS) {
      const lk = w.toLowerCase();
      if (lk === key || used.has(lk)) continue;
      const e = MINI[lk];
      if (!e || !e.senses.length) continue;
      const c = score(w, e);
      if (c) extra.push(c);
    }
    cands = [...poolCands, ...extra];
  }
  cands.sort((a, b) => b.n - a.n);

  // 前 6 名里随机取 3：既保证是像的，又不至于每次都是同一组
  const picked = shuffle(cands.slice(0, Math.max(3, Math.min(6, cands.length)))).slice(0, 3);

  const choices: Choice[] = shuffle([
    { word: me.word, pos: targetPos, text: targetText, right: true },
    ...picked.map((c) => ({
      word: c.w,
      pos: c.pos,
      text: c.text,
      sub: undefined,
      right: false,
    })),
  ]);

  return { word: me.word, sense, phonetic: me.phonetic, choices };
}
