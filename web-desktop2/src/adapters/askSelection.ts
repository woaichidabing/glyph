/**
 * 历史会话「批量删除」的选择逻辑。
 *
 * 为什么要单独抽成纯函数：选择集合的增删最容易出幽灵状态——
 * 会话被删了、列表刷新了，选中集合里却还留着那个 id，再点删除就会
 * 把它重复发给后端。这类 bug 只有在离线测试里才打得出来。
 */

/** 勾选 / 取消勾选一个。已选就摘掉，没选就加上。 */
export function toggleId(selected: string[], id: string): string[] {
  return selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
}

/** 全选 / 全不选一组：只动这一组，别组已选的一个不碰。 */
export function selectAll(selected: string[], ids: string[], on: boolean): string[] {
  if (on) {
    const merged = [...selected];
    for (const id of ids) if (!merged.includes(id)) merged.push(id);
    return merged;
  }
  const drop = new Set(ids);
  return selected.filter((x) => !drop.has(x));
}

/** 列表变了（删完 / 刷新 / 换会话）之后，把已经不存在的 id 剔掉。 */
export function pruneSelection(selected: string[], aliveIds: string[]): string[] {
  const alive = new Set(aliveIds);
  return selected.filter((x) => alive.has(x));
}

/** 这批删除里有没有当前会话：有的话前端必须切走并清掉时间线。 */
export function deletesCurrent(selected: string[], currentId: string | null): boolean {
  return !!currentId && selected.includes(currentId);
}

/** 计数只数还存在的，避免「已选 3 个」里混着两个早就不在的。 */
export function countSelected(selected: string[], aliveIds: string[]): number {
  return pruneSelection(selected, aliveIds).length;
}
