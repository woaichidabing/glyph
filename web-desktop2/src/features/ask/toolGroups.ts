/**
 * 渲染单元切分（纯函数：无 React、无 DOM）。
 *
 * 为什么在**渲染层**分组、而不是把「组」写进时间线：
 * 时间线的 `confirmed` 边界（重试只能撤它后面的）与 `turn_retry` 裁剪逻辑都建立在
 * **扁平节点序列**上（见 `askTimeline.ts` 头注三条硬规矩与 `reduce` 的 turn_retry 分支）。
 * 往序列里塞容器节点，会波及 reducer 的每个 case；只在渲染层聚合则一行 reducer 都不用动，
 * 而且这层还能被离线单测钉死。
 *
 * 分组规则（设计稿 §3.2 / §5）：
 * - **相邻**的非卡片类工具节点合成一节（顺序即发生顺序，被别的节点隔开就是两节）；
 * - 挂卡片的工具（`get_quiz` / `propose_plan` / `propose_wordbook_add`）单独成节，
 *   **不并入工具块**，并把它两侧切断。
 */

import type { TimelineNode, ToolNode } from "../../adapters/askTimeline";

/**
 * 挂了卡片的工具：卡片本身就是结果，再叠一行「出题练习完成」是冗余。
 * `ask_user` 不在此列——它在时间线里是 `ask` 节点，本来就不是工具节点。
 */
export const CARD_ONLY_TOOLS: ReadonlySet<string> = new Set([
  "get_quiz",
  "propose_plan",
  "propose_wordbook_add",
]);

export type RenderUnit =
  | { kind: "node"; node: TimelineNode }
  | { kind: "toolGroup"; id: string; nodes: ToolNode[] }
  /** 一整轮的过程：思考 + 工具 + 中间那句旁白，折成头部一行。
   *  这一轮**最后一个动作之后**的文字不进这里，它作为交付单独露出。 */
  | { kind: "process"; id: string; nodes: TimelineNode[] };

/**
 * 把时间线节点切成渲染单元。
 *
 * 组 id 取**轮次锚点 + 段序号**（`p-<user id>-<序>`）：节点增删后未被影响的组 id 不变，
 * 因此挂在 id 上的展开态不会错挂到别的组。
 */
export function toRenderUnits(nodes: TimelineNode[]): RenderUnit[] {
  const units: RenderUnit[] = [];
  // 一轮 = 两条 user 消息之间（或首尾）的所有节点；同轮内容收进同一个过程块。
  let pending: TimelineNode[] = [];
  // 过程块的身份用**轮次锚点**（这一轮的那条 user 消息），不用"第一个节点"：
  // 轮的组成会随流式追加变化（旁白被并进正文、思考先于工具到达），拿首节点编号时
  // id 会变，整块随之重建——用户看到的就是「闪一下」，折叠状态也会被重置。
  let anchor = "root";
  // 同一轮可能被卡片/提问/终态切成多段，段序号保证 id 唯一（否则 React key 撞车、
  // 展开状态会串到另一段上）。
  let groupIndex = 0;

  const flush = () => {
    if (pending.length === 0) return;
    const batch = pending;
    pending = [];

    // 交付 = 最后一个工具/思考**之后**的那段文字。
    // 工具后面没文字（被中断）就没有交付，别把旁白当成回答。
    let lastAction = -1;
    for (let i = batch.length - 1; i >= 0; i -= 1) {
      if (batch[i].kind === "tool" || batch[i].kind === "reasoning") {
        lastAction = i;
        break;
      }
    }
    let finalIndex = -1;
    for (let i = batch.length - 1; i > lastAction; i -= 1) {
      if (batch[i].kind === "text") {
        finalIndex = i;
        break;
      }
    }

    const process = batch.filter((_, i) => i !== finalIndex);
    if (process.length > 0) {
      units.push({ kind: "process", id: "p-" + anchor + "-" + groupIndex, nodes: process });
      groupIndex += 1;
    }
    if (finalIndex >= 0) {
      units.push({ kind: "node", node: batch[finalIndex] });
    }
  };

  for (const node of nodes) {
    // 用户消息、要用户点的卡片、产物卡片：露在外面，并把过程切断
    const standalone =
      node.kind === "user" ||
      node.kind === "ask" ||
      node.kind === "finalize" ||
      // 终态必须在过程块外：失败/取消/未确认要靠它显示原因和「重连」，
      // 折进过程块就等于把错误藏起来了。
      node.kind === "terminal" ||
      (node.kind === "tool" && CARD_ONLY_TOOLS.has(node.name));
    if (standalone) {
      flush();
      if (node.kind === "user") {
        anchor = node.id;
        groupIndex = 0;
      }
      units.push({ kind: "node", node });
      continue;
    }
    pending.push(node);
  }
  flush();
  return units;
}
