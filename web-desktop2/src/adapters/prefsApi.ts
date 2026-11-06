/* 跨重启的设置：存在后端数据目录里。
 *
 * 为什么不用 localStorage：桌面端每次启动端口不同，浏览器按"网址"隔离存储，
 * 换端口就读不到上次的设置（深色、上次会话等会全部复位）。 */
export async function loadPrefs(): Promise<Record<string, unknown>> {
  try {
    const res = await fetch("/api/prefs");
    if (!res.ok) return {};
    const data = await res.json();
    return data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export function savePref(key: string, value: unknown): void {
  try {
    void fetch("/api/prefs", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key, value }),
    });
  } catch { /* 存不上不阻塞界面 */ }
}
