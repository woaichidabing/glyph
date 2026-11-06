export type StorageInfo = {
  root: string;
  default_root: string;
  legacy_root: string;
  writable: boolean;
  bytes: number;
  sessions: number;
  copied_files?: number;
};

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : "请求失败";
    throw new Error(detail);
  }
  return body as T;
}

export function getStorage(): Promise<StorageInfo> {
  return call<StorageInfo>("/api/storage");
}

export function changeStorageRoot(path: string, migrate: boolean): Promise<StorageInfo> {
  return call<StorageInfo>("/api/storage/root", {
    method: "POST",
    body: JSON.stringify({ path, migrate }),
  });
}

export function migrateStorage(source?: string): Promise<StorageInfo> {
  return call<StorageInfo>("/api/storage/migrate", {
    method: "POST",
    body: JSON.stringify(source ? { source } : {}),
  });
}

export function openStorageFolder(): Promise<{ ok: boolean; root: string }> {
  return call<{ ok: boolean; root: string }>("/api/storage/open", { method: "POST" });
}
