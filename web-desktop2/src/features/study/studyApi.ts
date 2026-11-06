/** 背词状态后端接口：整份快照读写。 */
export async function loadStudyState(): Promise<unknown | null> {
  const res = await fetch("/api/study/state");
  if (!res.ok) throw new Error("背词状态没读上");
  const data = (await res.json()) as { state?: unknown };
  return data.state ?? null;
}

export async function saveStudyState(state: unknown): Promise<void> {
  const res = await fetch("/api/study/state", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(state),
    keepalive: true,
  });
  if (!res.ok) throw new Error("背词状态没保存上");
}
