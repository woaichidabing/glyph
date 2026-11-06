/**
 * /ask 时间线的纯逻辑层：事件 → 节点序列。
 *
 * 为什么单独抽出来、不写在组件里：
 * 时间线最容易错的地方（顺序、重试边界、终态）全都发生在这里，
 * 而它们跟 React 一点关系都没有。抽成纯函数才能在离线测试里被反复打，
 * 不用真的连后端跑一遍模型（那是要花钱的）。
 *
 * 三条硬规矩（对应执行规格 §4）：
 * 1. **顺序即发生顺序**。节点按事件到达的顺序排，不能靠「一个工具数组 + 一段 answer」
 *    再拼出次序——那样并行工具倒序完成时一定会串位。
 * 2. **工具结果只更新已有步骤**，不追加第二个节点。靠 call_id 找人。
 * 3. **终态只认后端给的 finish_reason**。没收到合法终态就是「状态未确认」，
 *    绝不因为流断了就说这一轮成功了。
 */

import {
  parseAskArgs,
  resolveAskResult,
  type AskNode,
} from "./askCard";

/* ask_user 卡片节点类型从 askCard 模块统一出口，展示层仍只从 askTimeline 引 */
export type { AskNode, AskOption, AskCardState } from "./askCard";

/* ---------------- 事件（与后端 src/glyph/events.py 对齐） ---------------- */

export type AskEvent =
  | { type: "run_started" }
  /**
   * 服务端回话：这一轮**实际写进了哪个会话**（每轮第一帧）。
   * 前端每个标签页各持一个 id，服务端按 id 翻本子；id 打不开时它会开新的并把新 id
   * 发回来——前端必须跟着改，否则会一直拿旧 id 来，每次都被当成"打不开"而新建一个。
   */
  | { type: "session_bound"; sessionId: string }
  | { type: "queued"; message?: string }
  | { type: "text_delta"; text: string }
  /**
   * 思考增量（阶段 2 思考采集，后端 reasoning_delta）。只展示、折成思考块；
   * 不进账本、不进模型上下文、不影响终态——与 text_delta 是两条流，绝不合并。
   */
  | { type: "reasoning_delta"; text: string }
  | { type: "tool_notice"; message: string }
  /** 整条 assistant 消息收完整了：用它把暂存的文字钉死，形成「已确认边界」 */
  | { type: "assistant_message"; text: string }
  | { type: "tool_call"; id: string; name: string; args?: Record<string, unknown> }
  | {
      type: "tool_result";
      id: string;
      ok: boolean;
      output?: unknown;
      error?: string;
      durationMs?: number;
    }
  | {
      type: "web_search";
      phase: string;
      searchId?: string;
      query?: string;
      sources: { title?: string; url: string }[];
    }
  | { type: "turn_retry"; reason?: string; message?: string }
  | {
      /**
       * 上下文压缩进度（后端 CompactionEvent）。前端只在「需要用户等」时露一行进行提示：
       * started / retrying(level=l3)=浓缩进行中（转圈 + 一句固定的话）；
       * l2=无感装箱、done=浓缩完成、retrying(level=emergency)=已当场减重继续，全部静默无痕；
       * failed/skipped=撤掉进行中行（致命失败后端会另发 error 事件，由错误卡给人话）。
       * 不拿后端 message / level / token 做展示，避免把 L2/L3、水位等术语暴露给用户。
       */
      type: "compaction";
      phase: "started" | "l2" | "l3" | "retrying" | "done" | "failed" | "skipped" | string;
      level?: string;
      message?: string;
      beforeTokens?: number;
      afterTokens?: number;
      boundaryId?: string;
    }
  | {
      /** finishReason 来自后端 FinishReason 枚举；缺了就不许当成成功 */
      type: "result";
      text?: string;
      finishReason?: string;
      /** 最后一轮实际发送的 input tokens，上下文用量环的分子；模型没回 usage 时为 null */
      contextInputTokens?: number | null;
      /** 当前大脑的上下文窗口，环的分母（后端对未知型号回退保守值，恒正） */
      contextWindow?: number;
      /**
       * 本次 run 的用量（输入 / 输出 / 其中命中缓存）。模型没回 usage 时为 null，
       * 或 cachedTokens 为 null 表示"厂商没给这个字段"——未知与 0 是两件事。
       */
      usage?: TurnUsage | null;
    }
  | { type: "error"; message: string };

/* ---------------- 节点 ---------------- */

export type ToolState = "running" | "ok" | "fail" | "cancelled";

/** 终态：正常完成 / 用户取消 / 失败 / 没收到合法终态 */
export type TerminalState = "done" | "cancelled" | "failed" | "unconfirmed";

export type TimelineAttachment = {
  id: string;
  name: string;
  kind: "image" | "file";
  /** 后端给图片编的号；OCR 结果要靠它找对原图，不能一律取第一张 */
  imageId?: string;
  /** 本次会话内刚上传的图才有本地预览地址；历史回放里没有，界面照实说「原图暂不可用」 */
  url?: string;
};

export type UserNode = {
  id: string;
  kind: "user";
  text: string;
  attachments: TimelineAttachment[];
  sortKey: number;
  /**
   * 界面动作替用户发出的指令（不是用户亲手打的字）：如 OCR 核对台点「确认批改」。
   * 带它时时间线只渲染一行居中灰条（text 仍照常发给模型），不显示蓝色用户气泡。
   * 历史回放不带它（服务端存的是真实消息文本），仅活流本地节点使用。
   */
  note?: string;
};

export type TextNode = {
  id: string;
  kind: "text";
  text: string;
  /** 已被 assistant_message 确认。未确认的半截内容在重试时要整段撤掉 */
  confirmed: boolean;
  /** 属于哪一轮 run；最终消息只清理同一轮的半截文字 */
  runId?: number;
  sortKey: number;
};

export type ToolNode = {
  id: string;
  kind: "tool";
  callId: string;
  name: string;
  args?: Record<string, unknown>;
  state: ToolState;
  startedAt: number;
  durationMs?: number;
  output?: unknown;
  error?: string;
  sortKey: number;
};

/**
 * 思考块（阶段 2 思考采集）：模型的思考过程，折起展示的一条展示型节点。
 *
 * `done=false` 表示还在流式思考（组头「正在思考…」）；模型转入正文 / 工具 / 收尾时
 * 收成 `true`（组头「已完成思考」）。它是**展示型内容**：不进账本、不进模型上下文，
 * 再长也不占上下文预算。未确认（还没跨过 assistant_message 边界）的思考在 turn_retry
 * 时整块撤掉——它是半截尝试的一部分，与半截正文同命运。
 */
export type ReasoningNode = {
  id: string;
  kind: "reasoning";
  text: string;
  done: boolean;
  /** 属于哪一轮 run */
  runId?: number;
  /** 开始思考的墙上时间（毫秒）；历史回放没有 */
  startedAt?: number;
  /** 实际思考了多久；完成时才写入，历史回放没有 */
  durationMs?: number;
  sortKey: number;
};

export type NoticeNode = {
  id: string;
  kind: "notice";
  message: string;
  sortKey: number;
};

/**
 * 压缩进行中给用户看的那一句固定的话。用户不是程序员，不暴露 L2/L3、水位、token 等术语，
 * 也不采用后端自带的 message（措辞可能带术语、且各阶段不一致）。
 */
export const COMPACTING_MESSAGE = "对话有点长了，我先整理一下前面的内容，马上继续…";

/**
 * 上下文压缩进行提示：只在「需要用户等」的浓缩进行中出现一行转圈 + 一句固定的话。
 * L2 装箱、浓缩完成、413 应急已当场减重继续都静默无痕，所以节点只有「进行中」一种形态，
 * 也不携带 level / message / token。只活在当前流式时间线里，刷新后从后端历史重放不会重现。
 */
export type CompactionNode = {
  id: string;
  kind: "compaction";
  sortKey: number;
};

/**
 * 一次 run 的用量（后端 result 事件带回）。cachedTokens = null 表示厂商没给这个字段
 * （未知），与 0（一个都没命中）是两件事；界面据此决定"要不要解释这一轮"。
 */
export type TurnUsage = {
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number | null;
};

export type TerminalNode = {
  id: string;
  kind: "terminal";
  state: TerminalState;
  label: string;
  sortKey: number;
  /** 本轮用量；历史回放没有（后端未回灌），此时状态条整行不显示 */
  usage?: TurnUsage;
};

/** 定稿讲解清单里的一条（只装渲染要用的字段） */
export type FinalizeExplain = {
  original?: string;
  corrected?: string;
  explanationZh?: string;
  tier?: string;
};

/** 用户对某次批改的定稿（v2.1 #69）：历史里显示一张定稿卡，靠 callId 关联回那次批改。 */
export type FinalizeNode = {
  id: string;
  kind: "finalize";
  /** 对应的 grade_text 调用 id（grade_call_id） */
  callId: string;
  finalText: string;
  comment: string;
  keptCount: number;
  sortKey: number;
  /** 篇名（本次定稿即时写入才有；历史回放后端不回灌，退化为泛称） */
  title?: string;
  /** 后端 build_marked_segments 分段，用于定稿正文绿色高亮（历史回放无） */
  marks?: unknown;
  /** 折叠讲解清单（历史回放无，只展示全文） */
  explanations?: FinalizeExplain[];
};

export type TimelineNode =
  | UserNode
  | TextNode
  | ReasoningNode
  | ToolNode
  | AskNode
  | NoticeNode
  | CompactionNode
  | TerminalNode
  | FinalizeNode;

export type Timeline = {
  nodes: TimelineNode[];
  /** 已确认边界：前 confirmed 个节点是钉死的，重试只能撤它后面的 */
  confirmed: number;
  /** 单调递增，给节点发稳定 ID 与排序键 */
  seq: number;
  /** 当前 run 编号；最终消息按整轮合并半截文字 */
  runId: number;
  /** 重试的临时提示（瞬时，不落盘、不进节点） */
  retryNotice: string | null;
};

export const emptyTimeline = (): Timeline => ({
  nodes: [],
  confirmed: 0,
  seq: 0,
  runId: 0,
  retryNotice: null,
});

/* ---------------- 终态映射 ---------------- */

/**
 * 后端 FinishReason 只有四个取值（final_text / max_iterations / cancelled / failed）。
 * 认不出来的（包括整个字段缺失）一律算「未确认」——
 * 把未知终态当成成功，界面就会把半截回复说成答完了。
 */
/**
 * 这一轮是不是「什么都没产生」：**正常结束（done），但时间线上没有任何可看的内容**。
 *
 * 为什么需要它：后端 FinishReason 里没有"空回复"这一档 —— 模型回空消息也走
 * final_text，前端只看到一个 done。不给提示的话，用户会以为界面卡住了。
 *
 * 口径：
 *   · 没有终态（还在跑）、或终态不是 done（出错 / 取消 / 未确认）→ 不算空，那些另有 UI
 *   · 有正文（非空白）/ 工具结果 / 定稿卡 / 反问卡 → 不算空，那都是给用户看的内容
 *   · 只有思考 → **算空**：思考是过程，不是产出
 */
export function isEmptyTurn(nodes: readonly TimelineNode[]): boolean {
  // 只看**最后一轮**：以最后一条 user 为界，往后找最后一条 terminal。
  // （早先版本用 nodes.find 取第一个 terminal，多轮之后永远命中第一轮，判空失效。）
  // 没有 user 节点（系统开场、历史截断）时，从最后一个 terminal 之后算起：
  // 否则整条历史都算"这一轮"，永远判不出空回复。
  let start = 0;
  let sawUser = false;
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    if (nodes[i].kind === "user") { start = i; sawUser = true; break; }
  }
  // 没有 user 节点时保持旧口径（整条时间线就是这一轮）：
  // 这时若从 terminal 起算，会把正文排除在外，反而误判成"空回合"。
  if (!sawUser) start = 0;
  const turn = nodes.slice(start);
  let terminal: Extract<TimelineNode, { kind: "terminal" }> | undefined;
  for (const n of turn) {
    if (n.kind === "terminal") terminal = n;
  }
  if (!terminal || terminal.state !== "done") return false;
  return !turn.some((n) => {
    switch (n.kind) {
      case "text":
        return n.text.trim() !== "";
      case "tool":
      case "finalize":
      case "ask":
        return true;
      default:
        return false;
    }
  });
}

export function terminalOf(finishReason: string | undefined): TerminalState {
  switch (finishReason) {
    case "final_text":
    case "max_iterations":
      return "done";
    case "cancelled":
      return "cancelled";
    case "failed":
      return "failed";
    default:
      return "unconfirmed";
  }
}

/* ---------------- 归约器 ---------------- */

const hasTerminal = (nodes: TimelineNode[]): boolean =>
  nodes.length > 0 && nodes[nodes.length - 1].kind === "terminal";

/** 轮次结束/失败/取消：所有还在等用户回答的卡片必须收掉，已答的保留为收合态 */
function closePendingAsks(nodes: TimelineNode[]): TimelineNode[] {
  return nodes.map((n) =>
    n.kind === "ask" && n.state === "waiting" ? { ...n, state: "dismissed" as const } : n,
  );
}

/** 本轮（最后一个 user 之后）有没有文字节点：result.text 只在完全没有正文时才兜底 */
function hasTextSinceUser(nodes: TimelineNode[]): boolean {
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    const n = nodes[i];
    if (n.kind === "user") return false;
    if (n.kind === "text") return true;
  }
  return false;
}

/**
 * 思考阶段结束：把尾部仍在「思考中」的块收成完成态（模型已转入正文 / 工具 / 收尾）。
 * 只可能收尾巴那一个——思考块仅在它是最后一个节点时才会被增量续写（见 reasoning_delta 分支）。
 */
function settleReasoning(nodes: TimelineNode[], now: number = Date.now()): TimelineNode[] {
  const last = nodes[nodes.length - 1];
  if (last && last.kind === "reasoning" && !last.done) {
    const durationMs = last.startedAt === undefined ? undefined : Math.max(0, now - last.startedAt);
    return nodes.map((n, i) =>
      i === nodes.length - 1 ? { ...last, done: true, durationMs } : n,
    );
  }
  return nodes;
}

/** 搜索终态判定：completed / no_result 为终态；**带来源才算成功，无来源一律失败**，
 *  这样即便后端误发「completed 但空来源」，前端也不会显示 ✓完成、不会诱导编造。 */
function searchOutcome(phase: string, sources: { url: string }[]): ToolState | null {
  if (phase !== "completed" && phase !== "no_result") return null;
  return sources.length > 0 ? "ok" : "fail";
}

function upsertSearch(
  nodes: TimelineNode[],
  ev: Extract<AskEvent, { type: "web_search" }>,
  sortKey: number,
  startedAt: number,
  /** 完成时刻由调用方给（reduce 收到的 now），不在纯函数里偷看墙上时钟 */
  now: number,
): TimelineNode[] {
  let idx = ev.searchId
    ? nodes.findIndex((n) => n.kind === "tool" && n.callId === ev.searchId)
    : -1;
  if (idx < 0) {
    // 千问的 in_progress/searching 帧没有 id；将它们归并到最近一张仍在运行的搜索卡。
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
      const n = nodes[i];
      if (n.kind === "tool" && n.name === "web_search" && n.state === "running") {
        idx = i;
        break;
      }
    }
  }
  const found = idx >= 0 ? nodes[idx] : undefined;
  if (found && found.kind === "tool") {
    const query = ev.query ?? (found.args?.query as string | undefined);
    const outcome = searchOutcome(ev.phase, ev.sources);
    if (!outcome) {
      if (!ev.query || query === found.args?.query) return nodes;
      return nodes.map((n, i) =>
        i === idx && n.kind === "tool" ? { ...n, args: { ...n.args, query } } : n,
      );
    }
    const next: ToolNode = {
      ...found,
      callId: ev.searchId ?? found.callId,
      state: outcome,
      output: { query, sources: ev.sources },
      error: outcome === "fail" ? "没有找到来源，未获取到联网内容" : undefined,
      durationMs: now - found.startedAt,
    };
    return nodes.map((n, i) => (i === idx ? next : n));
  }
  // 配对不上（空壳帧 / 重试后步骤被撤）：不静默丢来源，补一个节点兜住
  const outcome = searchOutcome(ev.phase, ev.sources);
  const node: ToolNode = {
    id: ev.searchId ? `ws-${ev.searchId}` : `ws-${sortKey}`,
    kind: "tool",
    callId: ev.searchId ?? `ws-${sortKey}`,
    name: "web_search",
    args: ev.query ? { query: ev.query } : undefined,
    state: outcome ?? "running",
    startedAt,
    output: outcome ? { query: ev.query, sources: ev.sources } : undefined,
    error: outcome === "fail" ? "没有找到来源，未获取到联网内容" : undefined,
    sortKey,
  };
  return [...nodes, node];
}

/** 单个事件打进时间线。纯函数：不改入参，返回新的一份。 */
export function reduce(tl: Timeline, ev: AskEvent, now: number = Date.now()): Timeline {
  const { nodes } = tl;

  switch (ev.type) {
    case "queued":
      return tl;

    case "run_started": {
      // 新一轮开始（典型：等待反问时用户又发了消息，旧流被掐断）：上一轮遗留的
      // 等待卡不可能再收到结果，收掉防死卡。queued 不在此列——被排队的是新请求，
      // 挂着的卡属于仍在跑的任务。
      // 新一轮开始：上一轮没确认的半截文字 / 思考不能再留在时间线上，
      // 否则第二次思考会接在旧半截后面，出现两个「已思考」块。
      const cleaned = nodes.filter(
        (n, i) => i < tl.confirmed || (n.kind !== "reasoning" && n.kind !== "text") || n.kind === "text" && n.confirmed,
      );
      const closed = closePendingAsks(cleaned);
      return { ...tl, runId: tl.runId + 1, nodes: closed };
    }

    case "session_bound":
      // 服务端告知"这一轮写在哪个会话"：是显示层的会话归属，不是一条可展示的消息，
      // 时间线不收（useAgentStream 会据此校正本地 sessionId）。
      return tl;

    case "text_delta": {
      if (ev.text === "") return tl;
      const last = nodes[nodes.length - 1];
      if (last && last.kind === "text" && !last.confirmed && last.runId === tl.runId) {
        return {
          ...tl,
          nodes: nodes.map((n, i) =>
            i === nodes.length - 1 ? { ...last, text: last.text + ev.text } : n,
          ),
        };
      }
      return {
        ...tl,
        seq: tl.seq + 1,
        nodes: [
          ...settleReasoning(nodes, now),
          { id: `t-${tl.seq}`, kind: "text", text: ev.text, confirmed: false, runId: tl.runId, sortKey: tl.seq },
        ],
      };
    }

    case "reasoning_delta": {
      const last = nodes[nodes.length - 1];
      if (last && last.kind === "reasoning" && !last.done && last.runId === tl.runId) {
        // 同一段思考还在流：继续往它身上加字（同 text_delta 的续写规矩）
        return {
          ...tl,
          nodes: nodes.map((n, i) =>
            i === nodes.length - 1 ? { ...last, text: last.text + ev.text } : n,
          ),
        };
      }
      return {
        ...tl,
        seq: tl.seq + 1,
        nodes: [
          ...nodes,
          { id: `r-${tl.seq}`, kind: "reasoning", text: ev.text, done: false, runId: tl.runId, startedAt: now, sortKey: tl.seq },
        ],
      };
    }

    case "tool_notice":
      return {
        ...tl,
        seq: tl.seq + 1,
        nodes: [
          ...settleReasoning(nodes, now),
          { id: `n-${tl.seq}`, kind: "notice", message: ev.message, sortKey: tl.seq },
        ],
      };

    case "assistant_message": {
      // 最终消息到达：清掉本轮所有未确认的文字，再把完整正文放到最后。
      // 不能只检查最后一个节点——思考块可能插在半截文字和最终回复之间。
      let next = settleReasoning(nodes, now);
      next = next.filter(
        (n) => !(n.kind === "text" && !n.confirmed && n.runId === tl.runId),
      );
      if (ev.text) {
        next = [
          ...next,
          { id: `t-${tl.seq}`, kind: "text", text: ev.text, confirmed: true, runId: tl.runId, sortKey: tl.seq },
        ];
      }
      return { ...tl, seq: tl.seq + 1, nodes: next, confirmed: next.length, retryNotice: null };
    }

    case "tool_call": {
      // ask_user 不渲染成普通工具行：合法参数 → 等待中的决策卡；
      // 参数非法 → dismissed 占位（不显示），等 steer 结果配对，也防止孤儿结果补出工具行。
      if (ev.name === "ask_user") {
        const parsed = parseAskArgs(ev.args);
        const node: AskNode = parsed
          ? {
              id: `ask-${ev.id}`,
              kind: "ask",
              callId: ev.id,
              question: parsed.question,
              options: parsed.options,
              state: "waiting",
              sortKey: tl.seq,
            }
          : {
              id: `ask-${ev.id}`,
              kind: "ask",
              callId: ev.id,
              question: "",
              options: [],
              state: "dismissed",
              sortKey: tl.seq,
            };
        return { ...tl, seq: tl.seq + 1, nodes: [...settleReasoning(nodes, now), node] };
      }
      const node: ToolNode = {
        id: `c-${ev.id}`,
        kind: "tool",
        callId: ev.id,
        name: ev.name,
        args: ev.args,
        state: "running",
        startedAt: now,
        sortKey: tl.seq,
      };
      return { ...tl, seq: tl.seq + 1, nodes: [...settleReasoning(nodes, now), node] };
    }

    case "tool_result": {
      // ask_user 卡片靠 call.id 与结果配对：answered 收成已答态，其余（steer/no_answer/
      // 失败/坏形态）一律收卡。已收口的卡片幂等忽略重复结果。
      const askIdx = nodes.findIndex((n) => n.kind === "ask" && n.callId === ev.id);
      if (askIdx >= 0) {
        const ask = nodes[askIdx] as AskNode;
        if (ask.state !== "waiting") return tl;
        const r = resolveAskResult(ev.output, ev.ok);
        const next: AskNode =
          r.state === "answered"
            ? { ...ask, state: "answered", answer: r.answer, answerKind: r.kind }
            : { ...ask, state: "dismissed" };
        return { ...tl, nodes: nodes.map((n, i) => (i === askIdx ? next : n)) };
      }
      const idx = nodes.findIndex((n) => n.kind === "tool" && n.callId === ev.id);
      if (idx === -1) {
        // 找不到对应步骤（配对帧缺失等）也不能把结果扔掉
        return {
          ...tl,
          seq: tl.seq + 1,
          nodes: [
            ...nodes,
            {
              id: `c-${ev.id}`,
              kind: "tool",
              callId: ev.id,
              name: "tool",
              state: ev.ok ? "ok" : "fail",
              startedAt: now,
              durationMs: ev.durationMs,
              output: ev.output,
              error: ev.error,
              sortKey: tl.seq,
            },
          ],
        };
      }
      return {
        ...tl,
        nodes: nodes.map((n, i) => {
          if (i !== idx || n.kind !== "tool") return n;
          return {
            ...n,
            state: ev.ok ? "ok" : "fail",
            durationMs: ev.durationMs,
            output: ev.output,
            error: ev.error,
          };
        }),
      };
    }

    case "web_search":
      return {
        ...tl,
        seq: tl.seq + 1,
        nodes: upsertSearch(settleReasoning(nodes, now), ev, tl.seq, now, now),
      };

    case "compaction": {
      const hasWorking = (ns: TimelineNode[]): boolean =>
        ns.some((n) => n.kind === "compaction");
      const dropWorking = (ns: TimelineNode[]): TimelineNode[] =>
        ns.filter((n) => n.kind !== "compaction");
      // 撤掉进行中行并静默：本来就没有时原样返回（幂等，不产生多余渲染）。
      const dismissWorking = (): Timeline => {
        if (!hasWorking(nodes)) return tl;
        const next = dropWorking(nodes);
        return { ...tl, nodes: next, confirmed: next.length };
      };
      // 浓缩进行中：没有就补一行（固定文案由展示层给，节点不带任何术语）；已有则幂等。
      const ensureWorking = (ns: TimelineNode[]): TimelineNode[] =>
        hasWorking(ns)
          ? ns
          : [...ns, { id: `cmp-${tl.seq}`, kind: "compaction" as const, sortKey: tl.seq }];

      if (ev.phase === "started" || (ev.phase === "retrying" && ev.level !== "emergency")) {
        // started：L3 写手请求前发出，要等几十秒，给一行进行中提示。
        // retrying(level=l3)：材料对半砍后重发写手，浓缩仍在进行，保持这一行、不换术语文案。
        if (hasWorking(nodes)) return tl;
        const next = ensureWorking(nodes);
        return { ...tl, seq: tl.seq + 1, nodes: next, confirmed: next.length };
      }

      if (ev.phase === "done" || ev.phase === "failed" || ev.phase === "skipped") {
        // done=浓缩完成：整行消失，不留「已压缩」痕迹。
        // failed/skipped：撤掉进行中行；致命失败后端会另发 error 事件，由终态错误卡变红。
        return dismissWorking();
      }

      if (ev.phase === "retrying") {
        // 走到这里 level 必为 emergency：已当场组好减重视图并立刻重发本轮，属于「已默默处理完」，
        // 撤掉可能存在的进行中行、静默不打扰（l3 的 retrying 已在上面归为进行中）。
        return dismissWorking();
      }

      // l2（无感装箱）以及任何未识别 phase（后端从不单独发 l3）：一律静默，不动时间线。
      return tl;
    }

    case "turn_retry": {
      // 只撤「还没落账」的那部分：未确认的半截文字 + 还在跑的工具。
      // 已经拿到结果的工具属于既成事实（后端账本里已经有了），必须留下——
      // 旧实现把整个回合清空，前几轮的结果会一起丢掉。
      const kept: TimelineNode[] = [];
      for (const [index, n] of nodes.entries()) {
        if (n.kind === "tool" && n.state === "running") continue;
        // 等待中的反问卡随本轮一起作废重来；已回答的是既成事实，保留
        if (n.kind === "ask" && n.state === "waiting") continue;
        if (n.kind === "notice" && index >= tl.confirmed) continue;
        // 未确认的思考随本轮一起作废（已确认的留着——那是上一轮真实发生过的过程）
        if (n.kind === "reasoning" && index >= tl.confirmed) continue;
        kept.push(n);
      }
      while (kept.length > 0) {
        const last = kept[kept.length - 1];
        if (last.kind === "text" && !last.confirmed) kept.pop();
        else break;
      }
      return {
        ...tl,
        nodes: kept,
        confirmed: kept.length,
        retryNotice: ev.message ?? "这一轮推倒重来，正在重新处理",
      };
    }

    case "result": {
      if (hasTerminal(nodes)) return tl; // 已有终态（error 先到了）：不许把它改成成功
      let next = settleReasoning(closePendingAsks(nodes));
      if (ev.text && !hasTextSinceUser(nodes)) {
        next = [...next, { id: `t-${tl.seq}`, kind: "text", text: ev.text, confirmed: true, sortKey: tl.seq }];
      }
      const state = terminalOf(ev.finishReason);
      const label =
        state === "cancelled"
          ? "已停止"
          : state === "failed"
            ? "这一轮没能完成"
            : state === "unconfirmed"
              ? "连接中断，状态未确认"
              : "";
      next = [
        ...next,
        {
          id: `e-${tl.seq}`,
          kind: "terminal",
          state,
          label,
          sortKey: tl.seq,
          ...(ev.usage ? { usage: ev.usage } : {}),
        },
      ];
      return { ...tl, seq: tl.seq + 1, nodes: next, confirmed: next.length, retryNotice: null };
    }

    case "error": {
      if (hasTerminal(nodes)) return tl;
      const next = [
        ...settleReasoning(closePendingAsks(nodes)),
        {
          id: `e-${tl.seq}`,
          kind: "terminal" as const,
          state: "failed" as const,
          label: ev.message,
          sortKey: tl.seq,
        },
      ];
      return { ...tl, seq: tl.seq + 1, nodes: next, confirmed: next.length, retryNotice: null };
    }
  }
}

/** 整条流跑完却没收到任何终态事件：退出等待，如实说「未确认」 */
export function finalizeUnconfirmed(tl: Timeline): Timeline {
  if (hasTerminal(tl.nodes)) return tl;
  // 连接中断：等待中的反问卡再点也送不到了，一并收掉
  const next = [
    ...settleReasoning(closePendingAsks(tl.nodes)),
    {
      id: `e-${tl.seq}`,
      kind: "terminal" as const,
      state: "unconfirmed" as const,
      label: "连接中断，状态未确认",
      sortKey: tl.seq,
    },
  ];
  return { ...tl, seq: tl.seq + 1, nodes: next, confirmed: next.length, retryNotice: null };
}

/** 用户手动停止：已在飞的工具收成「已取消」，不用红色故障提示冒充 */
export function markCancelled(tl: Timeline): Timeline {
  const nodes = settleReasoning(
    closePendingAsks(
      tl.nodes.map((n) =>
        n.kind === "tool" && n.state === "running" ? { ...n, state: "cancelled" as const } : n,
      ),
    ),
  );
  const next = [
    ...nodes,
    {
      id: `e-${tl.seq}`,
      kind: "terminal" as const,
      state: "cancelled" as const,
      label: "已停止",
      sortKey: tl.seq,
    },
  ];
  return { ...tl, seq: tl.seq + 1, nodes: next, confirmed: next.length, retryNotice: null };
}

/* ---------------- 历史回放 → 时间线 ---------------- */

export type ReplayItemLike = {
  kind: string;
  seq: number;
  ts?: string;
  text?: string;
  tool_name?: string;
  call_id?: string;
  /** 工具**被宣告**时的序号；没有就退回 seq（旧记录兼容） */
  call_seq?: number | null;
  ok?: boolean;
  output?: unknown;
  error?: string | null;
  arguments?: Record<string, unknown>;
  duration_ms?: number;
  status?: string | null;
  finish_reason?: string | null;
  // 仅 kind=finalize
  final_text?: string;
  kept_ids?: string[];
};

const IMAGE_FACT_RE = /（用户上传了一张图片：([^，]+)，image_id:\s*([^）]+)）/g;
const FILE_FACT_RE = /（用户上传了文件：([^，]+)，保存在\s*([^，]+)，需要时可读取）/g;

/**
 * 后端把「用户发了图」记成一句事实陈述混在正文里（不是真的消息附件），
 * 回放时要把它拆出来，才能显示缩略图、并让 OCR 结果对上正确的那张图。
 */
export function parseUserFacts(raw: string): {
  text: string;
  attachments: TimelineAttachment[];
} {
  const attachments: TimelineAttachment[] = [];
  let text = raw;

  const imgRe = new RegExp(IMAGE_FACT_RE.source, "g");
  let m: RegExpExecArray | null;
  while ((m = imgRe.exec(raw))) {
    attachments.push({ id: `img-${attachments.length}`, name: m[1], kind: "image", imageId: m[2].trim() });
  }
  text = text.replace(IMAGE_FACT_RE, "");

  const fileRe = new RegExp(FILE_FACT_RE.source, "g");
  const files: TimelineAttachment[] = [];
  while ((m = fileRe.exec(raw))) {
    files.push({ id: `file-${files.length}`, name: m[1], kind: "file" });
  }
  text = text.replace(FILE_FACT_RE, "");

  return { text: text.replace(/\n{2,}/g, "\n").trim(), attachments: [...attachments, ...files] };
}

/** 后端回放 → 时间线。工具按「被宣告的顺序」就位，不看结果落盘的先后。 */
export function replayToTimeline(items: ReplayItemLike[]): Timeline {
  const raw: (TimelineNode & { order: number })[] = [];

  items.forEach((it, i) => {
    const seq = typeof it.seq === "number" ? it.seq : i;
    if (it.kind === "user") {
      const { text, attachments } = parseUserFacts(it.text ?? "");
      raw.push({ id: `u-${seq}`, kind: "user", text, attachments, sortKey: seq, order: seq });
    } else if (it.kind === "assistant") {
      raw.push({
        id: `t-${seq}`,
        kind: "text",
        text: it.text ?? "",
        confirmed: true,
        sortKey: seq,
        order: seq,
      });
    } else if (it.kind === "reasoning") {
      // 思考过程（阶段 2）：还原成折起的思考块。落盘过的思考一定是完整的一轮（done）。
      raw.push({
        id: `r-${seq}`,
        kind: "reasoning",
        text: it.text ?? "",
        done: true,
        sortKey: seq,
        order: seq,
      });
    } else if (it.kind === "tool") {
      if (it.tool_name === "ask_user") {
        // 历史里只回灌「已回答」的反问，显示为收合态卡片；
        // steer/no_answer 那一问用户从没见过，不留节点。
        const r = resolveAskResult(it.output ?? undefined, it.ok !== false);
        if (r.state === "answered") {
          const parsed = parseAskArgs(it.arguments);
          const order = it.call_seq ?? seq;
          raw.push({
            id: `ask-${it.call_id ?? seq}`,
            kind: "ask" as const,
            callId: it.call_id ?? String(seq),
            question: parsed?.question ?? "",
            options: parsed?.options ?? [],
            state: "answered" as const,
            answer: r.answer,
            answerKind: r.kind,
            sortKey: order,
            order,
          });
        }
      } else {
        raw.push({
          id: `c-${it.call_id ?? seq}`,
          kind: "tool",
          callId: it.call_id ?? String(seq),
          name: it.tool_name ?? "tool",
          args: it.arguments,
          state: it.ok === false ? "fail" : "ok",
          startedAt: 0,
          durationMs: it.duration_ms,
          output: it.output ?? undefined,
          error: it.error ?? undefined,
          sortKey: it.call_seq ?? seq,
          // 排序用「被宣告的顺序」：并行工具谁先跑完谁先落盘，直接按 seq 会串位
          order: it.call_seq ?? seq,
        });
      }
    } else if (it.kind === "status") {
      // 后端目前只为 cancelled / failed 发 status 条目（正常完成不额外发）。
      // 认不出状态名的兜底顺序：先看 status，再看 finish_reason，都没有才说「未确认」——
      // 免得后端将来多一个状态名，界面把它错报成断线。
      let state: TerminalState;
      if (it.status === "failed") state = "failed";
      else if (it.status === "cancelled") state = "cancelled";
      else if (it.finish_reason) state = terminalOf(it.finish_reason);
      else state = "unconfirmed";
      raw.push({
        id: `e-${seq}`,
        kind: "terminal",
        state,
        label: it.text ?? (state === "cancelled" ? "已停止" : "这一轮没能完成"),
        sortKey: seq,
        order: seq,
      });
    } else if (it.kind === "finalize") {
      // 用户定稿成果（后端同一 call_id 只回灌最新一条），定位在那次批改之后
      raw.push({
        id: `f-${it.call_id ?? seq}`,
        kind: "finalize",
        callId: it.call_id ?? String(seq),
        finalText: it.final_text ?? "",
        comment: it.text ?? "",
        keptCount: Array.isArray(it.kept_ids) ? it.kept_ids.length : 0,
        sortKey: seq,
        order: seq,
      });
    }
  });

  // 稳定排序：只把「结果落盘晚但宣告早」的工具挪回原位
  const nodes = raw
    .map(({ order, ...rest }) => ({ ...rest, sortKey: order }))
    .sort((a, b) => a.sortKey - b.sortKey);

  return { nodes, confirmed: nodes.length, seq: nodes.length, runId: 0, retryNotice: null };
}

/* ---------------- 右侧工作区的开关状态机 ---------------- */

export type WorkspaceMode = "closed" | "side" | "max";

/**
 * 收回 / 并排 / 放大 / 还原。
 * 「放大后直接收回 → 再打开」必须回到并排，而不是又变回放大（验收 D）。
 */
export function nextWorkspace(
  mode: WorkspaceMode,
  action: "open" | "close" | "maximize" | "restore",
): WorkspaceMode {
  switch (action) {
    case "open":
      return mode === "closed" ? "side" : mode;
    case "close":
      return "closed";
    case "maximize":
      return mode === "side" ? "max" : mode === "closed" ? "side" : "max";
    case "restore":
      return mode === "max" ? "side" : mode;
  }
}

/**
 * 用户手动收回后，同一个任务的后续更新不再自动弹开；换一个任务可以再开。
 * key 用工具调用的 id（稳定，不靠 Date.now）。
 */
export function shouldAutoOpen(dismissedKey: string | null, key: string | null): boolean {
  return !!key && dismissedKey !== key;
}
