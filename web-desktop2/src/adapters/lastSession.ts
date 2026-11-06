/**
 * 「上次查看的会话」指针：刷新 /ask 后自动恢复（B-T10-1）。
 *
 * 会话本体由后端磁盘持久化；这里只存一个轻量指针（localStorage），
 * 跨后端重启也还在。恢复前必须用 {@link sessionToRestore} 确认该会话仍存在，
 * 否则指针可能指向已被删除 / 损坏的会话。
 */
import type { SessionSummary } from "./sessionApi";

const LAST_KEY = "glyph.session.last.v1";

/**
 * 决定刷新后要恢复哪个会话：
 * - 指针为空 → 不恢复（空欢迎页）；
 * - 指针指向的会话不在列表里（已删 / 损坏 / 尚未拉到）→ 不恢复；
 * - 否则返回该会话 id。
 */
export function sessionToRestore(
  lastId: string | null,
  sessions: SessionSummary[],
): string | null {
  if (!lastId) return null;
  return sessions.some((s) => s.session_id === lastId) ? lastId : null;
}

export function getLastSessionId(): string | null {
  try {
    return localStorage.getItem(LAST_KEY);
  } catch {
    return null;
  }
}

export function setLastSessionId(id: string | null): void {
  try {
    if (id) localStorage.setItem(LAST_KEY, id);
    else localStorage.removeItem(LAST_KEY);
  } catch {
    /* 隐私模式 / 配额满：放弃这一次落盘，不影响当前会话 */
  }
}
