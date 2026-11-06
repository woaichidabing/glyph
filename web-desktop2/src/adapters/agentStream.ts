/**
 * 活动流适配器：后端 SSE → 时间线事件。
 *
 * 事件逐字对齐后端 `src/glyph/events.py`，**不改造、不补全、不猜测**：
 *   run_started 开始 · queued 排队 · text_delta 逐字上屏 ·
 *   reasoning_delta 思考过程（只展示，前端折成思考块）·
 *   assistant_message 本轮完整消息（用来钉死"已确认边界"）·
 *   tool_call / tool_result 工具步骤（按 call_id 配对）·
 *   web_search 平台搜索过程（只展示）· turn_retry 本轮作废重来 ·
 *   compaction 上下文压缩进度（装箱/浓缩/应急精简的状态行）·
 *   result 终态（带 finish_reason）· error 循环级错误。
 *
 * ⚠️ 后端不可用时**必须如实报错**，绝不静默切换成本地假回复——
 * 那会让用户以为模型真的回答了，而实际上一个字都没发出去。
 */

import { uploadFile } from "./uploadRepo";
import { buildStudyContext } from "../features/study";
import type { AskEvent } from "./askTimeline";

export type StreamEvent = AskEvent;

export type AskPayload = {
  text: string;
  images: { id: string; name: string; blob: Blob; kind?: "image" | "file" }[];
  /** 由历史/记忆层拼出的上下文，后端接上后会消费 */
  memory?: string;
  /**
   * 这一轮必须写进哪个会话。服务端只有一个"当前本子"，不认 id 就会出现
   * 「这个标签页说的话写进另一个标签页的流水」。后端会回 session_bound 告知实际 id。
   */
  sessionId?: string;
};

/* ---------------- SSE 读取 ---------------- */

/**
 * 空闲看门狗阈值：后端在长耗时工具静默期每 15s 发一帧心跳（注释帧也算字节），
 * 容忍约 3 次心跳丢失；超过仍收不到任何字节即判定连接挂死，快速报错而不是无限转圈。
 */
const STREAM_IDLE_MS = 45_000;

/** 建连阶段（请求还没被后端接受）网络抖动的有限重试次数；流一旦开始就不再自动重试。 */
const CONNECT_MAX_RETRY = 2;

/** 解析一个 SSE 分块（可能含多行），返回可上屏事件；注释心跳 / 坏数据自然忽略。 */
function parseSSEPart(part: string): AskEvent[] {
  const out: AskEvent[] = [];
  for (const line of part.split("\n")) {
    const t = line.trim();
    if (!t.startsWith("data:")) continue; // ": ping" 注释帧在此忽略，但它的字节已喂饱看门狗
    const raw = t.slice(5).trim();
    if (!raw || raw === "[DONE]") continue;
    try {
      const ev = JSON.parse(raw) as Record<string, unknown>;
      const mapped = mapEvent(ev);
      if (mapped) out.push(mapped);
    } catch {
      /* 一行坏数据不该中断整条流 */
    }
  }
  return out;
}

/** 长时间收不到任何字节（连心跳都没有）：连接挂死，交上层走"状态未确认 + 重试"。 */
export class StreamIdleTimeoutError extends Error {}

async function* readSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<AskEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let idleTimer: ReturnType<typeof setTimeout> | undefined;

  // 每次读取都与空闲超时竞速：收到任意字节（含心跳注释）就重新计时；
  // 超时直接判挂死并结束（不再继续读），上层据此提示"未确认 + 重试"，而不是永久 running。
  const readWithTimeout = (): Promise<{ done: boolean; value?: Uint8Array }> =>
    new Promise((resolve, reject) => {
      idleTimer = setTimeout(
        () => reject(new StreamIdleTimeoutError("长时间没收到回应，连接可能已中断")),
        STREAM_IDLE_MS,
      );
      reader.read().then(
        (r) => {
          clearTimeout(idleTimer);
          resolve(r);
        },
        (e) => {
          clearTimeout(idleTimer);
          reject(e);
        },
      );
    });

  try {
    while (true) {
      const { done, value } = await readWithTimeout();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() ?? "";
      for (const part of parts) {
        for (const mapped of parseSSEPart(part)) yield mapped;
      }
    }
    // 末帧 flush：reader 正常结束时缓冲里可能还压着最后一帧（没有结尾空行），
    // 不补这一刀会丢掉终态 / 最后一条回复。
    if (buf.trim()) {
      for (const mapped of parseSSEPart(buf)) yield mapped;
    }
  } finally {
    clearTimeout(idleTimer);
    // 超时分支底层 read 可能仍挂起：先 cancel 释放 pending read，再 releaseLock，
    // 否则 releaseLock 会因"仍有 pending read"抛错。
    try {
      await reader.cancel();
    } catch {
      /* 已关闭 / 已释放都无所谓 */
    }
    reader.releaseLock?.();
  }
}

const uid = () => Math.random().toString(36).slice(2, 9);

function asObject(v: unknown): Record<string, unknown> | undefined {
  if (v === null || v === undefined) return undefined;
  if (typeof v === "string") {
    try {
      const p = JSON.parse(v) as unknown;
      return p && typeof p === "object" ? (p as Record<string, unknown>) : undefined;
    } catch {
      return undefined;
    }
  }
  if (typeof v === "object") return v as Record<string, unknown>;
  return undefined;
}

/** assistant_message 里的正文：后端给的是整条消息，文字块拼起来即可 */
function textOfMessage(ev: Record<string, unknown>): string {
  const msg = asObject(ev.message);
  if (!msg) return "";
  if (typeof msg.text === "string") return msg.text;
  const blocks = Array.isArray(msg.blocks)
    ? msg.blocks
    : Array.isArray(msg.content)
      ? msg.content
      : [];
  return blocks
    .map((b) => {
      const o = asObject(b);
      if (!o) return "";
      return typeof o.text === "string" ? o.text : "";
    })
    .join("");
}

function mapEvent(ev: Record<string, unknown>): AskEvent | null {
  const type = String(ev.type ?? "");
  switch (type) {
    case "session_bound":
      return { type: "session_bound", sessionId: String(ev.session_id ?? "") };

    case "text_delta":
    case "delta":
      return { type: "text_delta", text: String(ev.text ?? ev.delta ?? "") };

    case "reasoning_delta":
      return { type: "reasoning_delta", text: String(ev.text ?? ev.delta ?? "") };

    case "assistant_message":
      return { type: "assistant_message", text: textOfMessage(ev) };

    case "tool_notice":
      return { type: "tool_notice", message: String(ev.message ?? "我先处理一下，再把结果告诉你。") };

    case "tool_call": {
      // 后端发嵌套 call:{id,name,arguments}；同时兼容旧的扁平字段
      const call = asObject(ev.call) ?? {};
      return {
        type: "tool_call",
        id: String(call.id ?? ev.id ?? uid()),
        name: String(call.name ?? ev.name ?? "tool"),
        args: asObject(call.arguments ?? ev.arguments),
      };
    }

    case "tool_result":
      return {
        type: "tool_result",
        id: String(ev.call_id ?? ev.id ?? ""),
        ok: ev.ok !== false,
        durationMs: typeof ev.duration_ms === "number" ? ev.duration_ms : undefined,
        output: ev.output ?? undefined,
        error: ev.error ? String(ev.error) : undefined,
      };

    case "turn_retry":
      return {
        type: "turn_retry",
        reason: ev.reason ? String(ev.reason) : undefined,
        message: ev.message ? String(ev.message) : undefined,
      };

    case "compaction":
      // 上下文压缩进度（§14）：文案用后端 message，前端只透传 phase/level/水位，不编造。
      return {
        type: "compaction",
        phase: String(ev.phase ?? ""),
        level: ev.level ? String(ev.level) : undefined,
        message: ev.message ? String(ev.message) : undefined,
        beforeTokens: typeof ev.before_tokens === "number" ? ev.before_tokens : undefined,
        afterTokens: typeof ev.after_tokens === "number" ? ev.after_tokens : undefined,
        boundaryId: ev.boundary_id ? String(ev.boundary_id) : undefined,
      };

    case "queued":
      // 前一个任务还在跑，本请求已在服务端排队——透传给前端做提示，别静默丢弃
      return { type: "queued", message: ev.message ? String(ev.message) : undefined };

    case "run_started":
      return { type: "run_started" };

    case "web_search":
      return {
        type: "web_search",
        phase: String(ev.phase ?? ""),
        searchId: ev.search_id ? String(ev.search_id) : undefined,
        query: ev.query ? String(ev.query) : undefined,
        sources: Array.isArray(ev.sources)
          ? (ev.sources as Record<string, unknown>[]).map((s) => ({
              title: s.title ? String(s.title) : undefined,
              url: String(s.url),
            }))
          : [],
      };

    case "result": {
      const usageRaw = asObject(ev.usage);
      return {
        type: "result",
        text: ev.text ? String(ev.text) : undefined,
        // 终态原因必须透传：取消 / 失败 / 触顶 / 正常完成在界面上是四种含义
        finishReason: ev.finish_reason ? String(ev.finish_reason) : undefined,
        // 上下文用量环：分子是「最后一轮」input（不是逐轮累加的 usage），
        // 模型没回 usage 时显式 null——界面据此画空环，绝不谎报 0%。
        contextInputTokens:
          typeof ev.context_input_tokens === "number" ? ev.context_input_tokens : null,
        contextWindow: typeof ev.context_window === "number" ? ev.context_window : undefined,
        // 用量：后端没给、或没给缓存明细时一律 null（未知），界面据此不显示百分比
        usage:
          usageRaw && typeof usageRaw.input_tokens === "number"
            ? {
                inputTokens: usageRaw.input_tokens,
                outputTokens:
                  typeof usageRaw.output_tokens === "number" ? usageRaw.output_tokens : 0,
                cachedTokens:
                  typeof usageRaw.cached_tokens === "number" ? usageRaw.cached_tokens : null,
              }
            : null,
      };
    }

    case "error":
      return { type: "error", message: String(ev.message ?? "出了点岔子") };

    default:
      return null;
  }
}

/* ---------------- 出口 ---------------- */

const TEXT_DOC_RE = /\.(txt|md)$/i;

/** 后端连不上 / 返回错误时抛出人话原因，让界面如实报错 */
export class BackendUnavailableError extends Error {}

export async function* streamAsk(
  p: AskPayload,
  signal: AbortSignal,
): AsyncGenerator<AskEvent> {
  // txt / md 文档先走 /api/upload 落盘并拼事实；只有图片进 multipart 的 images
  const docs = p.images.filter((i) => i.kind === "file" || TEXT_DOC_RE.test(i.name));
  const imgs = p.images.filter((i) => !docs.includes(i));
  let text = p.text;
  const notes: string[] = [];
  for (const d of docs) {
    try {
      const up = await uploadFile({ blob: d.blob, name: d.name });
      if (!up.accepted || !up.path) {
        notes.push(`（文件 ${d.name} 未被接受：${up.reason ?? "未知原因"}）`);
      } else {
        notes.push(`（用户上传了文件：${d.name}，保存在 ${up.path}，需要时可读取）`);
      }
    } catch (e) {
      notes.push(`（文件 ${d.name} 上传失败：${(e as Error).message}）`);
    }
  }
  if (notes.length) text = `${text}${text ? "\n\n" : ""}${notes.join("\n")}`;

  const fd = new FormData();
  fd.append("text", text);
  // 认会话：让服务端把"当前本子"翻到这个 id 再跑（翻不到会开新的并回传新 id）
  if (p.sessionId) fd.append("session_id", p.sessionId);
  if (p.memory) fd.append("memory", p.memory);
  // T06：只读学习状态摘要。没选词书就是没数据（null），这时一个字段都不带，
  // 让后端根本没有这段可注入——Agent 拿不到就必须说不知道，不许自己编进度。
  const study = await buildStudyContext().catch(() => null);
  if (study) fd.append("study_context", JSON.stringify(study));
  imgs.forEach((img) => fd.append("images", img.blob, img.name));

  // 建连阶段网络抖动：请求还没被后端接受时有限次自动重试（此时重试幂等安全）；
  // 一旦 res 建立、开始读流就绝不自动重试——那可能重复跑工具 / 重复花额度。
  let res: Response | null = null;
  for (let attempt = 0; attempt <= CONNECT_MAX_RETRY; attempt++) {
    try {
      res = await fetch("/api/ask", { method: "POST", body: fd, signal });
      break;
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      if (attempt === CONNECT_MAX_RETRY) {
        throw new BackendUnavailableError("暂时连不上，请稍后重试。");
      }
      await new Promise((r) => setTimeout(r, 300 * 2 ** attempt));
    }
  }
  if (!res || !res.body) {
    throw new BackendUnavailableError("暂时连不上，请稍后重试。");
  }
  if (!res.ok) {
    const detail = await res
      .json()
      .then((d: { detail?: unknown }) => (typeof d.detail === "string" ? d.detail : ""))
      .catch(() => "");
    if (
      [401, 402, 403, 429].includes(res.status) ||
      /quota|exhausted|forbidden|unauthorized|invalid api key|额度|欠费|鉴权/i.test(detail)
    ) {
      // 额度 / 授权是确定性错误：别让用户干等重试，直接说清该换模型 / 查钥匙。
      throw new BackendUnavailableError(
        detail || "当前模型暂时用不了：额度或授权有问题，换一个模型再试。",
      );
    }
    throw new BackendUnavailableError(detail || "出了点问题，没能收到回复，请稍后再试。");
  }
  yield* readSSE(res.body);
}
