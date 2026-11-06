export type HomeKind = "new" | "review" | "wrong";
export type HomeBlock = { kind: HomeKind; total: number; done: number };

function selectable(b: HomeBlock, free: boolean): boolean {
  return b.total > 0 || (b.kind === "new" && free);
}

/** 错词也算功课；随心背没有“今日已毕”。 */
export function homeAllDone(blocks: HomeBlock[], free: boolean): boolean {
  if (free) return false;
  const active = blocks.filter((b) => b.total > 0);
  return active.length > 0 && active.every((b) => b.done >= b.total);
}

/** 主按钮永远指向还没做完的那一段，不拿完成的段冒充下一段。 */
export function pickPrimary(blocks: HomeBlock[], free: boolean, selected: HomeKind): HomeBlock | undefined {
  const pending = blocks.filter((b) => selectable(b, free) && b.done < b.total && !(b.kind === "new" && free));
  return pending.find((b) => b.kind === selected) ?? pending[0] ?? blocks.find((b) => selectable(b, free));
}
