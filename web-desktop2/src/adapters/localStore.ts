/** localStorage 的薄封装：读写都做 JSON 兜底，坏数据一律当作空，不让页面崩 */
export function readJSON<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function writeJSON(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 隐私模式或配额满：静默失败，功能降级为"本次会话内有效" */
  }
}
