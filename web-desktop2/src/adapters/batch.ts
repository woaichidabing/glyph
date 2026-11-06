/**
 * 单篇 / 多篇批改与 OCR 的批次领域模型（纯逻辑层）。
 *
 * 与 askTimeline 同一套思路：批次怎么组成、每一篇走到哪一步、失败如何隔离，
 * 这些最容易错的事跟 React 无关，全部写成纯函数，离线就能反复验证，
 * 不用真连后端跑模型（那是要花钱的）。
 *
 * 事实来源只有一个：askTimeline 归约出的时间线节点。
 * - 用户发的每张图片附件 = 一篇图片作文的诞生（例外：模型走 describe_image 看图路线
 *   时说明这张图不是作文，该篇随即从批次里摘掉）；
 * - ocr_image 工具调用按「被宣告的顺序」与未绑定的图片配对（后端 image_id 是
 *   uuid，前端事先不知道，同轮并行调用的顺序是唯一可靠的配对依据）；
 * - grade_text 调用的 args.text 与某篇转写稿归一化比对：匹配上就归入那一篇，
 *   匹配不上说明是一篇直接发文本的作文，单独成篇；
 * - finalize 节点（按 grade_call_id 关联）= 该篇定稿，批次随之关闭。
 *
 * 一次最多 3 篇（BATCH_MAX）；会话里还没定稿的篇自动算作同一批（会话累积）。
 */

import type { Timeline, TimelineNode, ToolNode, UserNode } from "./askTimeline";
import { extractScores, type ScoresDTO } from "./scoreCard";

export const BATCH_MAX = 3;

/** 篇的处理状态（货架步骤条与徽章据此渲染） */
export type ItemStatus =
  | "transcribing" // ocr_image 已认领、转写中（图片未被 OCR 认领前不产生篇）
  | "needs_review" // 转写完成，等人核对（确认闸：核对前不进批改）
  | "review_confirmed" // 已确认转写稿，等批改开始（前端确认闸临时态）
  | "ocr_failed" // 转写失败，可单独重试
  | "grading" // grade_text 进行中
  | "needs_decision" // 批改完成，等逐条拍板
  | "grade_failed" // 批改失败，可单独重试
  | "done"; // 已定稿

export type ItemOrigin = "image" | "text";

/** 后端 grade_text 出参里的单条批改（字段以后端 grading_models.Detection 为准） */
export type DetectionDTO = {
  id?: string | null;
  original?: string;
  corrected?: string;
  type?: string;
  tier?: string;
  explanation_zh?: string;
  source?: string;
  error_code?: string | null;
  start?: number | null;
  end?: number | null;
  aligned?: boolean;
  recheck?: unknown;
  [k: string]: unknown;
};

export type GradeOutput = {
  detections: DetectionDTO[];
  summary?: string;
  stats?: {
    words?: number;
    error_count?: number;
    suggestion_count?: number;
    total?: number;
  };
  /** 写作四维评分（内容/结构/准确/丰富）；模型没给时为 null */
  scores?: ScoresDTO | null;
  warnings?: string[];
};

export type LowConfidenceSpan = { start: number; end: number; text: string; reason: string };

export type BatchItem = {
  id: string;
  origin: ItemOrigin;
  /** 展示名：图片用文件名，文本用首行 */
  title: string;
  /** 本次会话刚上传的图片才有本地预览地址 */
  url?: string;
  /** 后端图片编号（ocr 出参回带；历史回放的用户事实里也可能带） */
  imageId?: string;
  status: ItemStatus;
  /** 配对不上图片的 ocr 调用：如实标注，界面不瞎配 */
  orphan?: boolean;
  ocrCallId?: string;
  gradeCallId?: string;
  /** 确认闸上的转写稿（可被用户直接修改） */
  transcript?: string;
  lowConfidence?: LowConfidenceSpan[];
  /** 用户核对确认后的稿子（可能改过 [?]）；确认后到批改开始前存在 */
  confirmedText?: string;
  warnings?: string[];
  grade?: {
    text: string;
    detections: DetectionDTO[];
    summary: string;
    stats: GradeOutput["stats"];
    scores?: ScoresDTO | null;
  };
  error?: string;
  final?: { finalText: string; keptCount: number; comment?: string };
};

export type Batch = {
  items: BatchItem[];
  /** 所有篇都已定稿（或根本没有篇）：批次关闭，货架隐去 */
  closed: boolean;
};

/** 输入区待发送的一张图（上限校验用） */
export type TrayItem = {
  id: string;
  kind: "image" | "text";
  /** 图片=文件名；文本=首句摘要（卡片标题） */
  name: string;
  /** kind=text 时的作文正文 */
  text?: string;
  url?: string;
  imageId?: string;
  size?: number;
  blob?: Blob;
};

/* ---------------- 纯函数 ---------------- */

/** 标题取正文首行 / 首句，截断 24 字符 */
export function titleFromText(text: string): string {
  const firstLine = text.split(/\n+/).map((s) => s.trim()).find(Boolean) ?? "";
  const firstSentence = firstLine.split(/(?<=[.!?])\s/)[0] ?? firstLine;
  return firstSentence.length > 24 ? `${firstSentence.slice(0, 24).trimEnd()}…` : firstSentence;
}

/** 粘贴/拖入的截图没有真实文件名，系统用 32 位十六进制哈希命名（如
 *  637b04f3e41d5a62616f222e8c11a5eb.jpg）——界面上露出哈希等于没名字，兜底成「图片作文」。 */
const HASH_NAME_RE = /^[0-9a-f]{32}$/i;
export const HASH_IMAGE_FALLBACK = "图片作文";

/** 图片篇标题：文件名去掉扩展名（IMG_2041.jpg → IMG_2041）；哈希名 → 图片作文 */
export function titleFromFileName(name: string): string {
  const base = name.replace(/\.[^.]+$/, "").trim();
  return HASH_NAME_RE.test(base) ? HASH_IMAGE_FALLBACK : base;
}

/** 批次内标题去重：两张粘贴截图都兜底成「图片作文」时，第二张叫「图片作文 2」。 */
function uniqueImageTitle(base: string, used: Set<string>): string {
  if (!used.has(base)) {
    used.add(base);
    return base;
  }
  for (let n = 2; ; n += 1) {
    const cand = `${base} ${n}`;
    if (!used.has(cand)) {
      used.add(cand);
      return cand;
    }
  }
}

/** 归一化：只保留小写字母/数字/撇号词序列。
 * 用来判断 grade 的文本是不是某篇转写稿——OCR 真机可能带斜杠分词符（`word / word`）、
 * 粘连标点（`word-,`）、连字符拼写，只折叠空白会认不出是同一篇（2026-10-13 真机：
 * 确认稿带斜杠、模型批改时去掉斜杠，结果被当成新作文开了第二个批改台）。 */
function normEssay(s: string): string {
  return (s.toLowerCase().match(/[a-z0-9']+/g) ?? []).join(" ");
}

/**
 * 两篇英文是不是同一份作文：全等最好；对不上时允许「长的包含短的」兜底
 * （用户核对时可能顺手改了一两个 [?]，agent 也可能只截正文）。
 * 短于 20 字符不做包含匹配，防止 "OK" 之类误归并到别的篇。
 */
export function sameEssay(a: string, b: string): boolean {
  const x = normEssay(a);
  const y = normEssay(b);
  if (!x || !y) return false;
  if (x === y) return true;
  const [longer, shorter] = x.length >= y.length ? [x, y] : [y, x];
  return shorter.length >= 20 && longer.includes(shorter);
}

type OcrOutput = {
  text: string;
  low_confidence?: LowConfidenceSpan[];
  warnings?: string[];
  image_id?: string;
};

function asOcrOutput(v: unknown): OcrOutput | undefined {
  const o = asRecord(v);
  if (!o || typeof o.text !== "string") return undefined;
  return {
    text: o.text as string,
    low_confidence: Array.isArray(o.low_confidence) ? (o.low_confidence as LowConfidenceSpan[]) : [],
    warnings: Array.isArray(o.warnings) ? (o.warnings as string[]) : [],
    image_id: typeof o.image_id === "string" ? (o.image_id as string) : undefined,
  };
}

function asRecord(v: unknown): Record<string, unknown> | undefined {
  if (v && typeof v === "object") return v as Record<string, unknown>;
  return undefined;
}

type NormalizedGradeOutput = Omit<GradeOutput, "summary" | "warnings" | "stats"> & {
  summary: string;
  warnings: string[];
  stats: GradeOutput["stats"];
};

function asGradeOutput(v: unknown): NormalizedGradeOutput | undefined {
  const o = asRecord(v);
  if (!o || !Array.isArray(o.detections)) return undefined;
  return {
    detections: o.detections as DetectionDTO[],
    summary: typeof o.summary === "string" ? o.summary : "",
    stats: asRecord(o.stats) as GradeOutput["stats"],
    scores: extractScores(o.scores),
    warnings: Array.isArray(o.warnings) ? (o.warnings as string[]) : [],
  };
}

/**
 * 从时间线推导当前批次（含跨回合尚未定稿的所有篇 = 会话累积）。
 * @param confirmed 前端确认闸记录：itemId → 用户确认的转写稿（可编辑过）；
 *                  needs_review 的篇命中后显示为 review_confirmed，批改一开始即失效。
 */
export function deriveBatch(tl: Timeline, confirmed: Record<string, string> = {}): Batch {
  const items: BatchItem[] = [];

  /**
   * 「待认领的图片」池：用户回合里的图片附件先进这里，**不直接生成批次篇**。
   *
   * 为什么改（2026-10-13 真机 BUG）：旧实现"图片一出现 = 一篇排队中的作文"，用户只发图
   * 还没说要干嘛，底部就冒出"排队中/这篇还没定稿"；而且先 describe 看图、后改口批改时，
   * describe 会把预占位删掉，OCR 回来配对不上只好建孤儿项（标题变图片编号、原图丢失）。
   * 新规矩：**图片不是作文，直到 ocr_image 认领它**（意图由工具行为证明，不由图片本身或
   * 用户一句"批改"推断——与 outbound.ts"没说的话不替他说"对齐）。
   */
  const pool: Array<{ itemId: string; title: string; url?: string; imageId?: string }> = [];
  const claimed = new Set<string>();
  const usedTitles = new Set<string>();

  const findByOcrCall = (id: string) => items.find((it) => it.ocrCallId === id);
  const findByGradeCall = (id: string) => items.find((it) => it.gradeCallId === id);

  for (const node of tl.nodes) {
    // 用户回合：图片只进待认领池（纯文本篇仍等 grade 调用出现时才算数，普通对话不产生篇）
    if (node.kind === "user") {
      const u = node as UserNode;
      u.attachments.filter((a) => a.kind === "image").forEach((a, idx) => {
        pool.push({
          itemId: `item-${u.id}-img-${idx}`,
          title: titleFromFileName(a.name) || "图片作文",
          url: a.url,
          imageId: a.imageId,
        });
      });
      continue;
    }

    if (node.kind === "tool") {
      const tool = node as ToolNode;

      if (tool.name === "describe_image") {
        // 看图路线（不是批改）：图片仍留在待认领池里——没有预占位可摘。
        // 用户之后改口要批改时，ocr_image 仍能按原附件认领（带文件名与原图地址）。
        continue;
      }

      if (tool.name === "ocr_image") {
        // 配对：① 重试（相同 image_id 的失败篇）精确领回；
        // ② 待认领池：先按 image_id 精确领，再按声明顺序领最早一张没被认领的图片。
        // 后端 image_id 是 uuid、活流附件事先不知道，顺序是多图首轮唯一可靠依据。
        const argImageId = typeof tool.args?.image_id === "string" ? String(tool.args.image_id) : undefined;
        let it = argImageId
          ? items.find((cand) => cand.origin === "image"
              && (cand.ocrCallId === undefined || cand.status === "ocr_failed")
              && cand.imageId === argImageId)
          : undefined;
        if (!it) {
          // 活流里前端附件通常还没有后端 image_id：先精确、配不上再按声明顺序认领最早一张
          const exactIdx = argImageId
            ? pool.findIndex((p) => !claimed.has(p.itemId) && p.imageId === argImageId)
            : -1;
          const poolIdx = exactIdx >= 0
            ? exactIdx
            : pool.findIndex((p) => !claimed.has(p.itemId));
          if (poolIdx >= 0) {
            const p = pool[poolIdx];
            claimed.add(p.itemId);
            it = {
              id: p.itemId,
              origin: "image",
              title: uniqueImageTitle(p.title, usedTitles),
              url: p.url,
              imageId: argImageId ?? p.imageId,
              status: "transcribing",
              ocrCallId: tool.callId,
            };
            items.push(it);
          }
        }
        if (!it) {
          // 配对不上（历史会话：图片事实缺失 / 调用数多于图片数）：不瞎配，挂孤儿项如实展示
          it = {
            id: `item-${tool.callId}`,
            origin: "image",
            title: argImageId ? argImageId.slice(0, 8) : "未知图片",
            imageId: argImageId,
            orphan: true,
            status: "transcribing",
            ocrCallId: tool.callId,
          };
          items.push(it);
        } else {
          // 认领军：新 call 接管这一篇，清掉上一轮失败痕迹
          it.ocrCallId = tool.callId;
          it.error = undefined;
        }
        if (tool.state === "running") {
          it.status = "transcribing";
        } else if (tool.state === "ok") {
          const out = asOcrOutput(tool.output);
          if (out) {
            it.status = "needs_review";
            it.transcript = out.text;
            it.lowConfidence = out.low_confidence;
            it.warnings = out.warnings;
            if (out.image_id) it.imageId = out.image_id;
          }
        } else if (tool.state === "fail") {
          it.status = "ocr_failed";
          it.error = tool.error ?? "转写失败";
        }
        continue;
      }

      if (tool.name === "grade_text") {
        const text = typeof tool.args?.text === "string" ? (tool.args.text as string) : "";
        // upsert：deriveBatch 对任意时刻的快照重算，工具节点可能还在 running，
        // 也可能已经是 ok/fail（同一节点被 tool_result 原地更新），两种快照都要成立。
        let it = findByGradeCall(tool.callId);
        if (!it) {
          // 先看这篇 grade 批改的是不是某张图的转写稿（含用户核对时小改、以及失败后重试的情形）
          const imageItem = items.find(
            (cand) => cand.origin === "image"
              && (!cand.gradeCallId || cand.status === "grade_failed")
              && !!cand.transcript
              // 比对稿优先级：本回合确认闸里的稿子（节点循环早于 confirmedText 回填，
              // 必须直接查 confirmed）> 上次 grade 请求稿 > 原始转写稿
              && sameEssay(confirmed[cand.id] ?? cand.grade?.text ?? (cand.transcript as string), text),
          );
          // 文本篇失败重试：同原文的新 call 认领回失败项，不新建第二篇
          const retryTextItem = imageItem ? undefined : items.find(
            (cand) => cand.origin === "text" && cand.status === "grade_failed"
              && !!cand.grade?.text && sameEssay(cand.grade.text, text),
          );
          if (imageItem || retryTextItem) {
            it = (imageItem ?? retryTextItem) as BatchItem;
            it.gradeCallId = tool.callId;
            it.error = undefined;
            it.grade = { text, detections: [], summary: "", stats: undefined };
          } else {
            it = {
              id: `item-${tool.callId}`,
              origin: "text",
              title: titleFromText(text) || "英语作文",
              status: "grading",
              gradeCallId: tool.callId,
              grade: { text, detections: [], summary: "", stats: undefined },
            };
            items.push(it);
          }
        }
        if (tool.state === "running") {
          it.status = "grading";
        } else if (tool.state === "ok") {
          const out = asGradeOutput(tool.output);
          if (out) {
            const gradeText = it.grade?.text || text;
            it.status = "needs_decision";
            it.grade = { ...out, text: gradeText };
            if (!it.title || it.title === "英语作文") it.title = titleFromText(gradeText) || it.title;
          }
        } else if (tool.state === "fail") {
          it.status = "grade_failed";
          it.error = tool.error ?? "批改失败";
        }
        continue;
      }
    }

    if (node.kind === "finalize") {
      const it = findByGradeCall(node.callId);
      if (it) {
        it.status = "done";
        it.final = { finalText: node.finalText, keptCount: node.keptCount, comment: node.comment };
      }
    }
  }

  // 确认闸覆盖：仍停在待核对、且用户已确认的篇转为「已确认 · 等批改」
  for (const it of items) {
    if (it.status === "needs_review" && confirmed[it.id]) {
      it.status = "review_confirmed";
      it.confirmedText = confirmed[it.id];
    }
  }

  // 没有篇时 every 为 true（空批次视为关闭，货架不出现）；有篇则全部定稿才算关闭
  const closed = items.every((it) => it.status === "done");
  return { items, closed };
}

/* ---------------- 批次规则（上限 / 单篇隐身 / 步骤条） ---------------- */

/** 还没定稿的篇数（失败待重试也算开放，仍占名额） */
export function openCount(items: BatchItem[]): number {
  return items.filter((it) => it.status !== "done").length;
}

/** 还剩几个名额（开放中的篇与输入区待发都占名额；已定稿的篇腾名额） */
export function capacityRemaining(items: BatchItem[], tray: TrayItem[]): number {
  return Math.max(0, BATCH_MAX - openCount(items) - tray.length);
}

/**
 * 还能不能往输入区再加一张：还有剩余名额时可以。
 * 问的是「追加动作发生前」，UI 逐张添加时每加一张问一次。
 */
export function canAddToTray(items: BatchItem[], tray: TrayItem[]): boolean {
  return capacityRemaining(items, tray) > 0;
}

/**
 * 货架是否出现：批次开放中且「在途篇 + 输入区待发」超过 1 篇。
 * 单篇永远是现在的样子（货架整块隐身）；全部定稿后货架也隐去，成果留在对话里。
 */
export function isMultiBatch(batch: Batch, tray: TrayItem[]): boolean {
  if (batch.closed) return false;
  return openCount(batch.items) + tray.length > 1;
}

/** 需要右翼工作面板的篇状态：转写待核对、已确认等批改、批阅待拍板 */
export const PANEL_STATES: ItemStatus[] = ["needs_review", "review_confirmed", "needs_decision"];

/** 失败待处理的篇状态：同样需要面板承接「重试这一篇」 */
export const RETRY_STATES: ItemStatus[] = ["ocr_failed", "grade_failed"];

/**
 * 右翼工作面板模式（单/多篇统一入口）：
 * - multi：多篇在途，出货架（含折叠态）；
 * - single：只有一篇且正卡在核对/拍板，面板直接铺满、不出现货架（单篇零打扰）；
 * - none：纯问答、处理中、全定稿——面板整块隐身。
 * 输入区还没发送的图不算批次（items 为空时不弹空货架）。
 */
export function workPanelMode(batch: Batch, tray: TrayItem[]): "none" | "multi" | "single" {
  if (batch.items.length === 0) return "none";
  if (isMultiBatch(batch, tray)) return "multi";
  const open = batch.items.filter((i) => i.status !== "done");
  if (open.length === 1 && [...PANEL_STATES, ...RETRY_STATES].includes(open[0].status)) return "single";
  return "none";
}

/**
 * 放大并排画布的栏数档位：开放中的篇各占一栏（含转写/批改中的占位栏），
 * 无开放篇（空批次或全定稿）→ null，画布不出现。批次上限 3，最多 n3。
 */
export function canvasGridClass(items: BatchItem[]): "n1" | "n2" | "n3" | null {
  const n = items.filter((i) => i.status !== "done").length;
  if (n === 0) return null;
  return n === 1 ? "n1" : n === 2 ? "n2" : "n3";
}

/**
 * 货架标题进度汇总：优先喊用户动手（失败 > 待核对 > 待拍板 > 处理中），
 * 有已定稿的附上 x/N 进度；全定稿直接说全部定稿。
 */
export function batchSummary(items: BatchItem[]): string {
  const total = items.length;
  const done = items.filter((x) => x.status === "done").length;
  const count = (s: ItemStatus) => items.filter((x) => x.status === s).length;
  const failed = count("ocr_failed") + count("grade_failed");
  const review = count("needs_review");
  const decision = count("needs_decision");
  const open = total - done;
  const tail = done > 0 && open > 0 ? ` · 已定稿 ${done}/${total}` : "";
  if (open === 0) return "全部定稿";
  if (failed > 0) return `${failed} 篇失败待处理${tail}`;
  if (review > 0) return `${review} 篇待核对${tail}`;
  if (decision > 0) return `${decision} 篇待拍板${tail}`;
  return `${open} 篇处理中${tail}`;
}

/* 货架阶段：徽章配色（wait/ocr/run/review/done/fail）与文案 */
export type StageKind = "wait" | "ocr" | "run" | "review" | "done" | "fail";

export function stageInfo(item: BatchItem): { kind: StageKind; label: string; detail: string } {
  switch (item.status) {
    case "transcribing":
      return { kind: "run", label: "转写中", detail: "正在把照片转成文字…" };
    case "needs_review": {
      const n = item.lowConfidence?.length ?? 0;
      return n > 0
        ? { kind: "ocr", label: "待核对", detail: `转写完成 · ${n} 处没看清，待核对` }
        : { kind: "ocr", label: "待核对", detail: "转写完成 · 待核对后批改" };
    }
    case "review_confirmed":
      return { kind: "done", label: "已确认", detail: "转写已确认 · 等批改" };
    case "ocr_failed":
      // 副标题只给稳定人话；后端原始错误保留在 item.error 里供重试 / 排查，不直接甩给用户
      return { kind: "fail", label: "转写失败", detail: "转写失败，可重试这一篇" };
    case "grading":
      return { kind: "run", label: "批改中", detail: item.origin === "image" ? "核对完成 · 正在批改" : "正在批改…" };
    case "grade_failed":
      return { kind: "fail", label: "批改失败", detail: "批改失败，可重试这一篇" };
    case "needs_decision":
      return { kind: "review", label: "待拍板", detail: "批改完成 · 等你逐条拍板" };
    case "done":
      return { kind: "done", label: "已定稿", detail: "已定稿" };
  }
}

/** 窄屏（<1020px）右翼面板被媒体查询整块藏起时，塞在输入区上方的入口描述。
 *  single 带那一篇的标题与阶段（失败动作为「重试」，其余「打开」）；multi 给篇数与待办汇总。
 *  只负责文案/色调，点击后进放大并排画布（canvas-mode）全屏处理。 */
export type NarrowEntry = {
  tone: StageKind;
  title: string;
  statusText: string;
  actionText: string;
};

export function narrowWorkEntry(batch: Batch, tray: TrayItem[]): NarrowEntry | null {
  const mode = workPanelMode(batch, tray);
  if (mode === "none") return null;

  if (mode === "single") {
    const it = batch.items.find((i) => i.status !== "done");
    if (!it) return null;
    const st = stageInfo(it);
    const failed = RETRY_STATES.includes(it.status);
    return {
      tone: st.kind,
      title: it.title,
      statusText: st.label,
      // 动作跟着状态走：失败是「重试」，其余就是「打开」面板（不说泛泛的「处理」）
      actionText: failed ? "重试" : "打开",
    };
  }

  // multi：色调按最该动手的来（失败 > 待核对 > 待拍板 > 处理中），与 batchSummary 优先级一致
  const failed = batch.items.some((i) => RETRY_STATES.includes(i.status));
  const review = batch.items.some((i) => i.status === "needs_review");
  const decision = batch.items.some((i) => i.status === "needs_decision");
  const tone: StageKind = failed ? "fail" : review ? "ocr" : decision ? "review" : "run";
  return {
    tone,
    title: `${batch.items.length} 篇作文`,
    statusText: batchSummary(batch.items),
    actionText: failed ? "重试" : "打开",
  };
}

/** 迷你步骤条：图片篇 5 步（转写/核对/批改/拍板/定稿），文本篇 3 步（批改/拍板/定稿） */
export function itemStep(item: BatchItem): { index: number; total: number; failed?: boolean } {
  if (item.origin === "image") {
    switch (item.status) {
      case "transcribing": return { index: 0, total: 5 };
      case "ocr_failed": return { index: 0, total: 5, failed: true };
      case "needs_review": return { index: 1, total: 5 };
      case "review_confirmed": return { index: 2, total: 5 };
      case "grading": return { index: 2, total: 5 };
      case "grade_failed": return { index: 2, total: 5, failed: true };
      case "needs_decision": return { index: 3, total: 5 };
      case "done": return { index: 4, total: 5 };
    }
  }
  switch (item.status) {
    case "grading": return { index: 0, total: 3 };
    case "grade_failed": return { index: 0, total: 3, failed: true };
    case "needs_decision": return { index: 1, total: 3 };
    case "done": return { index: 2, total: 3 };
    // 文本篇理论上不会出现 OCR 阶段，兜底落在批改步
    default: return { index: 0, total: 3 };
  }
}

/**
 * 把转写稿按没看清的 [?] 标记切段：conf=true 的段是标记本身（核对视图高亮、可点改），
 * 其余是普通文本。OCR 输出里没看清的位置就写死成 "[?]"。
 */
export function splitConfidenceMarkers(text: string): { t: string; conf: boolean }[] {
  const out: { t: string; conf: boolean }[] = [];
  const re = /\[\?\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ t: text.slice(last, m.index), conf: false });
    out.push({ t: m[0], conf: true });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ t: text.slice(last), conf: false });
  return out;
}
