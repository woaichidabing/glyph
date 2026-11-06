import { loadStudyState, saveStudyState } from "./studyApi";
import { getUpdatedAt, restoreSnapshot, snapshot } from "./studyRepo";

let hydrating = true;
let timer: ReturnType<typeof setTimeout> | null = null;

/** 启动时拉后端；远端没有就把本机旧数据推上去。 */
export async function hydrateStudy(): Promise<boolean> {
  try {
    const remote = await loadStudyState();
    if (remote !== null) {
      const remoteAt = Number((remote as { updatedAt?: unknown }).updatedAt) || 0;
      // 本机比远端新（离线背过）时不能被旧快照覆盖。
      if (remoteAt >= getUpdatedAt() && restoreSnapshot(remote)) return true;
    }
    await saveStudyState(snapshot());
    return false;
  } catch {
    return false;
  } finally {
    hydrating = false;
  }
}

/** 学习状态变化后合并成一次 PUT，不逐条打请求。 */
export function scheduleStudySync(): void {
  if (hydrating) return;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => {
    timer = null;
    void saveStudyState(snapshot()).catch(() => {});
  }, 350);
}

/** 刷新 / 关页面时尽量把最后一笔带出去，不等 350ms 的防抖。 */
if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    if (!timer) return;
    clearTimeout(timer);
    timer = null;
    void saveStudyState(snapshot()).catch(() => {});
  });
}
