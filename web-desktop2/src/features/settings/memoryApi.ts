/** 记忆的查看与清空（设置页用）。 */

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = "请求失败（" + res.status + "）";
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // 保持默认文案
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export async function getMemoryCount(): Promise<number> {
  const data = await call<{ count?: number }>("/api/memory");
  return typeof data.count === "number" ? data.count : 0;
}

export async function clearMemory(): Promise<number> {
  const data = await call<{ removed?: number }>("/api/memory/clear", { method: "POST" });
  return typeof data.removed === "number" ? data.removed : 0;
}

export async function getTrashStats(): Promise<{ count: number; bytes: number }> {
  const data = await call<{ count?: number; bytes?: number }>("/api/memory/trash");
  return { count: typeof data.count === "number" ? data.count : 0, bytes: typeof data.bytes === "number" ? data.bytes : 0 };
}

export async function purgeTrash(): Promise<{ removed: number; freed: number }> {
  const data = await call<{ removed?: number; freed?: number }>("/api/memory/trash", { method: "DELETE" });
  return { removed: typeof data.removed === "number" ? data.removed : 0, freed: typeof data.freed === "number" ? data.freed : 0 };
}
