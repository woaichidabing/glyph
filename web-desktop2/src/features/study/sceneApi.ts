/** 学习卡答后例句：例句库优先，服务端缓存 / 生成兜底。 */
export type Scene = {
  word: string;
  en: string;
  zh: string;
  source: "tatoeba" | "generated";
  pos?: string;
};

export async function fetchScene(
  word: string,
  pos = "",
  zh = "",
  refresh = false,
  signal?: AbortSignal,
): Promise<Scene | null> {
  const q = new URLSearchParams({ word, pos, zh });
  if (refresh) q.set("refresh", "1");
  try {
    const res = await fetch("/api/study/scene?" + q.toString(), { signal });
    if (!res.ok) return null;
    const data = (await res.json()) as { scene?: Scene | null };
    return data.scene ?? null;
  } catch {
    return null;
  }
}

export async function discardScene(word: string): Promise<void> {
  try {
    await fetch("/api/study/scene?" + new URLSearchParams({ word }).toString(), { method: "DELETE" });
  } catch {
    /* 缓存删不掉也不影响当前卡片 */
  }
}
