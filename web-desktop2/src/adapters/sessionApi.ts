/**
 * 后端会话接口（会话由后端磁盘持久化，前端只显示与切换）。
 * 对应测试台已跑通的 /api/session/current、/api/sessions* 端点。
 */

export type SessionSummary = {
  session_id: string;
  title: string;
  created_at: number | string;
  model?: string;
  is_current: boolean;
};

export type ReplayItem = {
  kind: "user" | "assistant" | "tool" | "status" | "finalize";
  seq: number;
  ts: string;
  text: string;
  tool_name?: string;
  call_id?: string;
  ok?: boolean;
  output?: Record<string, unknown> | null;
  error?: string | null;
  arguments?: Record<string, unknown>;
  duration_ms?: number;
  status?: string | null;
  finish_reason?: string | null;
  // 仅 kind=finalize：用户对某次批改（call_id=grade_call_id）的定稿
  final_text?: string;
  decisions?: Record<string, string>;
  overrides?: Record<string, string>;
  edited_ids?: string[];
  kept_ids?: string[];
};

export type SessionMessages = {
  messages: { role: string; text: string }[];
  replay: ReplayItem[];
  skipped_lines: number;
  /** 会话最后一轮的真实 input tokens（后端回灌）；老会话 / 无 usage 时为 null */
  last_input_tokens: number | null;
  /**
   * 本会话逐轮相加的用量（含命中缓存）：切回旧会话时状态条靠它回显。
   * 老会话 / 模型从没回过用量时为 null —— 状态条整行不显示，而不是显示 0。
   */
  usage?: {
    input_tokens: number | null;
    output_tokens: number | null;
    cached_tokens: number | null;
  } | null;
};

async function asJson<T>(res: Response): Promise<T> {
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON */
  }
  if (!res.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${res.status}）`);
  }
  return data as unknown as T;
}

export async function getCurrentSessionId(): Promise<string | null> {
  try {
    const res = await fetch("/api/session/current");
    if (!res.ok) return null;
    return ((await res.json()) as { session_id: string }).session_id;
  } catch {
    return null;
  }
}

export async function listSessionsApi(): Promise<SessionSummary[]> {
  const res = await fetch("/api/sessions");
  const data = await asJson<{ sessions: SessionSummary[] }>(res);
  return data.sessions ?? [];
}

export async function getSessionMessagesApi(sessionId: string): Promise<SessionMessages> {
  const res = fetch(`/api/sessions/${sessionId}/messages`);
  return asJson<SessionMessages>(await res);
}

export async function newSessionApi(): Promise<string> {
  const res = await fetch("/api/sessions/new", { method: "POST" });
  const data = await asJson<{ session_id: string }>(res);
  return data.session_id;
}

export async function openSessionApi(sessionId: string): Promise<string> {
  const res = await fetch("/api/sessions/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  const data = await asJson<{ session_id: string }>(res);
  return data.session_id;
}

export type DeleteResult = { deleted: string; new_session_id: string | null };

/** 批量删除：后端去重保序后逐个删，返回**真的删掉**的那些（不存在的会被跳过） */
export type BatchDeleteResult = { deleted: string[]; new_session_id: string | null };

export async function deleteSessionApi(sessionId: string): Promise<DeleteResult> {
  const res = await fetch(`/api/sessions/${sessionId}`, { method: "DELETE" });
  return asJson<DeleteResult>(res);
}

export async function batchDeleteSessionsApi(sessionIds: string[]): Promise<BatchDeleteResult> {
  const res = await fetch("/api/sessions/batch-delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_ids: sessionIds }),
  });
  return asJson<BatchDeleteResult>(res);
}

/**
 * created_at 归一化为毫秒时间戳。
 * 后端 /api/sessions 实际给的是 ISO 8601 字符串（如 2026-10-07T21:19:03+08:00），
 * 个别地方也可能给秒 / 毫秒数字，三种都要接住，否则分组与相对时间会算成 NaN。
 */
export function toMs(ts: number | string): number {
  let n: number;
  if (typeof ts === "number") {
    n = ts;
  } else {
    const parsed = new Date(ts).getTime(); // ISO 字符串
    n = Number.isNaN(parsed) ? Number(ts) : parsed; // 纯数字字符串兜底
    if (Number.isNaN(n)) n = Date.now(); // 实在解析不了，别让 NaN 污染分组 / 时间
  }
  return n < 1_000_000_000_000 ? n * 1000 : n;
}
