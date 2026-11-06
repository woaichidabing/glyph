/* ============================================================
   对话时间线：AI 正文 + 折叠组头（思考块 / 工具块）

   设计稿：docs/design/ask-transcript-render-design.md
   三条铁律（全文以此为准）：
   1. 折过程，不折结果——搜索来源内联、批改成品进右侧工作区；
   2. 任何状态都不自动展开（运行中 / 已完成 / 失败一律折起）；
   3. 重试归 agent——界面不挂「重试」按钮，只在真需要用户动手时给一个「导航」。

   结构：时间线仍由 adapters/askTimeline.ts 产出扁平节点序列（不改），
   这里用 adapters/toolGroups.ts 把它切成渲染单元，再交给渲染层。
   ============================================================ */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useStore } from "../../store";
import { useAgent } from "../../agent/AgentContext";
import {
  COMPACTING_MESSAGE,
  isEmptyTurn,
  type TimelineNode,
  type ToolNode,
  type ToolState,
} from "../../adapters/askTimeline";
import { toRenderUnits, CARD_ONLY_TOOLS, type RenderUnit } from "./toolGroups";
import { StepIcon } from "./stepIcons";
import { stepArgOf, stepInfoOf, stepMetaOf, stepOutputText } from "./stepMeta";
import {
  actionPhrase,
  buildToolGroupTitle,
  toolParam,
  type GroupItem,
  type GroupPhase,
} from "./toolPhrases";
import { classifyToolError, failSuffix } from "./toolErrorText";
import CollapseGroup from "../../components/CollapseGroup";
import ThinkingBlock from "../../components/ThinkingBlock";
import { StateIcon, IconRing } from "../../components/icons";
import { Markdown } from "../../components/Markdown";
import FinalizeCard from "../../components/FinalizeCard";
import AskCard from "./AskCard";
import { QuizCard, extractQuiz } from "../quiz";
import { PlanProposalCard, extractPlan } from "../plan";
import { WordbookProposalCard, extractWordbookProposal } from "../wordbook";

/** 状态 → 语义色类名。颜色只在 template.css 的 `.cgi` 四条里定，这里只给身份。 */
const TONE_CLASS: Record<ToolState, string> = {
  running: "run",
  ok: "ok",
  fail: "bad",
  cancelled: "stop",
};

/** 组头形态 → 图标状态 */
function iconStateOf(phase: GroupPhase): ToolState {
  if (phase === "running") return "running";
  if (phase === "failed") return "fail";
  if (phase === "cancelled") return "cancelled";
  return "ok";
}

/** 收起态的整组语义色（失败整组红边红字，被停中性灰） */
function toneOf(state: ToolState): "run" | "ok" | "bad" | "stop" {
  return TONE_CLASS[state] as "run" | "ok" | "bad" | "stop";
}

/** 联网搜索节点带回的来源（`upsertSearch` 把它写进 output.sources） */
function searchSources(node: ToolNode): { title?: string; url: string }[] {
  const out = node.output as { sources?: unknown } | undefined;
  const list = out?.sources;
  if (!Array.isArray(list)) return [];
  return (list as { title?: string; url: string }[]).filter(
    (s) => !!s && typeof s.url === "string" && s.url.length > 0,
  );
}

/** 来源行右栏只留域名 */
function domainOf(url: string): string {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/**
 * 工具节点 → 组头入参。
 * 失败项只带**人话后缀**（`toolErrorText`），原始错误串永远不上屏；
 * 联网搜索的计数口径是**来源个数**，不是调用次数。
 */
/**
 * 产出「用户能看的原文」的工具 —— 这些步骤行会多挂一个「看原文」。
 * 现在只有 ocr_image：它抄出来的英文原文就在右侧工作区里。
 * 刻意不列 grade_text（它的产出是批改结果，本来就在时间线上）。
 */
const READABLE_TOOLS: ReadonlySet<string> = new Set(["ocr_image"]);

function toGroupItems(nodes: ToolNode[]): GroupItem[] {
  return nodes.map((n) => {
    // 带上时间戳：组头那句「共 12s」靠它算（见 toolPhrases.groupElapsedMs）
    const item: GroupItem = {
      name: n.name,
      state: n.state,
      startedAt: n.startedAt,
      durationMs: n.durationMs,
    };
    if (n.state === "fail") {
      item.failReason = failSuffix(n.name, classifyToolError(n.error));
    }
    if (n.name === "web_search") {
      item.count = searchSources(n).length;
    }
    return item;
  });
}

/** 过程块头：已完成 · 5 步 · 2 分 14 秒 / 运行中… · 3 步 / 有失败时补一句 */
export function stepHeaderTitle(
  steps: TimelineNode[],
  failed: number,
  live: boolean,
): string {
  const n = steps.length;
  if (live) return "运行中… · " + n + " 步";
  const first = steps[0] as { startedAt?: number } | undefined;
  const last = steps[n - 1] as { startedAt?: number; durationMs?: number } | undefined;
  let elapsed = "";
  if (first?.startedAt && last?.startedAt) {
    const ms = last.startedAt + (last.durationMs ?? 0) - first.startedAt;
    const total = Math.round(ms / 1000);
    if (total >= 1) {
      elapsed =
        total >= 60
          ? " · " + Math.floor(total / 60) + " 分 " + (total % 60) + " 秒"
          : " · " + total + " 秒";
    }
  }
  return "已完成 · " + n + " 步" + elapsed + (failed > 0 ? "（" + failed + " 步失败）" : "");
}

/** 过程块：一行头 + 折起的每一步。 */
function ProcessBlock({
  title,
  tone,
  open,
  onToggle,
  children,
}: {
  title: string;
  tone: "done" | "live" | "fail";
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className={"proc proc-" + tone}>
      <button type="button" className="proc-head" onClick={onToggle} aria-expanded={open}>
        <span className="proc-caret" aria-hidden="true">{open ? "⌄" : "›"}</span>
        <span className="proc-dot" aria-hidden="true" />
        <span className="proc-title">{title}</span>
      </button>
      {open ? <div className="proc-steps">{children}</div> : null}
    </div>
  );
}

/** 单步：图标 + 动词 + 短参数 + 右侧信息；点开看输出（长内容截断）。 */
function StepRow({
  node,
  open,
  onToggle,
}: {
  node: ToolNode;
  open: boolean;
  onToggle: () => void;
}) {
  const meta = stepMetaOf(node.name);
  const arg = stepArgOf(node);
  const info = node.state === "fail" ? failSuffix(node.name, classifyToolError(node.error)) : stepInfoOf(node);
  const [showAll, setShowAll] = useState(false);
  const text = stepOutputText(node);
  const lines = text ? text.split("\n") : [];
  const HEAD = 8;
  const shown = (showAll ? lines : lines.slice(0, HEAD)).join("\n");
  const rest = Math.max(0, lines.length - HEAD);
  const expandable = lines.length > 0;
  return (
    <div className={"pstep" + (node.state === "fail" ? " pstep-fail" : "")}>
      <button
        type="button"
        className="pstep-head"
        onClick={expandable ? onToggle : undefined}
        aria-expanded={expandable ? open : undefined}
      >
        <span className="pstep-ic" aria-hidden="true">
          <StepIcon name={node.state === "fail" ? "alert" : meta.icon} />
        </span>
        <span className="pstep-verb">{meta.verb}</span>
        {arg ? <span className="pstep-arg">{arg}</span> : null}
        {info ? <span className="pstep-info">{info}</span> : null}
        {expandable ? (
          <span className="pstep-caret" aria-hidden="true">{open ? "⌄" : "›"}</span>
        ) : null}
      </button>
      {open && expandable ? (
        <>
          <pre className="pstep-out">{shown}</pre>
          {rest > 0 ? (
            <button type="button" className="pstep-more" onClick={() => setShowAll((v) => !v)}>
              {showAll ? "收起" : "展开其余 " + rest + " 行"}
            </button>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/** 工具组：一行组头 + 折起的逐项；来源属于结果，不折、直接露在下面 */
function ToolGroupBlock({
  unit,
  open,
  onToggle,
  onOpenPanel,
}: {
  unit: Extract<RenderUnit, { kind: "toolGroup" }>;
  open: boolean;
  onToggle: () => void;
  /** 打开右侧工作区。两处用它：额度用完时去换模型 / 步骤行「看原文」。 */
  onOpenPanel: () => void;
}) {
  const title = buildToolGroupTitle(toGroupItems(unit.nodes));
  if (!title) return null;

  const state = iconStateOf(title.phase);
  // 唯一需要用户动手的一类（额度 / 鉴权）：给的是「导航」，不是「重试」——
  // agent 重发还是同一个错，重试没用（设计稿 §7.2 / §8）。
  const needsModelSwap = unit.nodes.some(
    (n) => n.state === "fail" && classifyToolError(n.error) === "quota",
  );

  const sources = unit.nodes.flatMap((n) => (n.name === "web_search" ? searchSources(n) : []));

  return (
    <>
      <CollapseGroup
        variant="tool"
        tone={toneOf(state)}
        icon={<StateIcon state={state} className={toneOf(state)} />}
        title={title.text}
        open={open}
        onToggle={onToggle}
        action={
          needsModelSwap ? (
            <span className="lk" onClick={onOpenPanel}>
              去设置换模型 →
            </span>
          ) : null
        }
      >
        {unit.nodes.map((n) => {
          const param = toolParam(n.name, n.args);
          const tail =
            n.state === "fail"
              ? failSuffix(n.name, classifyToolError(n.error))
              : n.durationMs != null
                ? `${(n.durationMs / 1000).toFixed(1)}s`
                : "";
          return (
            <div className={`ctool${n.state === "fail" ? " err" : ""}`} key={n.id}>
              <span className="cgi">
                <StateIcon state={n.state} className={TONE_CLASS[n.state]} />
              </span>
              <span>{actionPhrase(n.name)}</span>
              {param ? <span className="param">{param}</span> : null}
              {/* 「看原文」只给**产出文本且跑成功**的步骤 —— 打开右侧工作区，
                  原文就在那儿。没有确定落点时宁可不出这一颗。 */}
              {READABLE_TOOLS.has(n.name) && n.state === "ok" ? (
                <button type="button" className="clink" onClick={onOpenPanel}>
                  看原文
                </button>
              ) : null}
              {tail ? <span className="st">{tail}</span> : null}
            </div>
          );
        })}
      </CollapseGroup>

      {sources.length > 0 ? (
        <div className="csrc">
          {sources.map((s, i) => (
            <a
              className="csrc-item"
              key={`${s.url}-${i}`}
              href={s.url}
              target="_blank"
              rel="noreferrer"
            >
              <span>{s.title || s.url}</span>
              <span className="csrc-dom">{domainOf(s.url)}</span>
            </a>
          ))}
        </div>
      ) : null}
    </>
  );
}

/* 用户回合：文本 + 附件缩略。
 * 带 note 的回合不是用户真的打的字，而是界面动作（如「在 OCR 核对台确认批改」）
 * 自动发出的指令——正文不冒充用户发言，只显示一行居中的小灰条。 */
function UserTurn({
  text,
  attachments,
  note,
}: {
  text: string;
  attachments: { id: string; name: string; kind: string; url?: string }[];
  note?: string;
}) {
  if (note) {
    return (
      <div className="finrow user-note" role="note">
        <span>{note}</span>
      </div>
    );
  }
  return (
    <div className="turn-user">
      <div className="msg user">
        {text}
      {attachments.length ? <br /> : null}
      {attachments.map((a) =>
        a.kind === "image" && a.url ? (
          <a key={a.id} className="attach-thumb" href={a.url} target="_blank" rel="noreferrer" title={a.name}>
            <img src={a.url} alt={a.name} />
          </a>
        ) : (
          <span key={a.id} className="attach">
            <svg
              width="11"
              height="11"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <path d="M14 2v6h6" />
            </svg>
            {a.name}
          </span>
        ),
        )}
      </div>
      <MessageActions text={text} />
    </div>
  );
}

/* 消息操作排：每条消息下面只留一颗「复制」。
   刻意不做的时候：
   · 朗读 —— 整段 TTS 念英文对批改场景没用，删掉（按用户要求，2026-10-18）；
   · 赞 / 踩 —— 质量靠金标回归量化（39 条金标 F0.5 快照），不靠用户点赞；
   · 重试 —— 铁律：重试归 agent，界面不挂重试按钮（真需要用户动手时给「导航」）。 */
function MessageActions({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const plain = text.trim();
  if (!plain) return null;

  const onCopy = () => {
    // 剪贴板不可用（非 https / 权限被拒）时静默失败 —— 不弹窗打扰，原文还在界面上
    navigator.clipboard?.writeText(text.trim()).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      },
      () => {},
    );
  };

  return (
    <div className="macts">
      <button type="button" className="mact" onClick={onCopy} title="复制这条">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
        {copied ? "已复制" : "复制"}
      </button>
    </div>
  );
}

/* 助手正文：不套头像不挂名牌；未确认的半截内容带光标 */
function AgentText({ text, caret }: { text: string; caret: boolean }) {
  return (
    <div className="turn-ai">
      <div className="msg ai">
        <Markdown text={text} />
        {caret ? <span className="caret" /> : null}
      </div>
      <MessageActions text={text} />
    </div>
  );
}

/* 终态：已停止 / 状态未确认（黄条）/ 失败（可重试的是整轮重连，不是某一步） */
function Terminal({
  node,
  onRetry,
}: {
  node: Extract<TimelineNode, { kind: "terminal" }>;
  onRetry?: () => void;
}) {
  // 成功终态（done）不产生任何 UI；只有 停止 / 未确认 / 失败 才需要给用户提示
  if (node.state === "done") {
    return null;
  }
  if (node.state === "cancelled") {
    return (
      <div className="finrow turn-ended">
        <span className="te-k">已取消</span>
        <span className="te-d">{node.label || "停在这儿了，前面跑完的都留着。"}</span>
      </div>
    );
  }
  if (node.state === "unconfirmed") {
    return (
      <div className="verdict miss">
        <span className="v-tag">!</span>
        {node.label || "连接中断，状态未确认"}
      </div>
    );
  }
  // 出错：先说人话、再安抚一句、最后给一条出路。
  // 只有「重连」一颗 —— 按铁律，界面不挂重试按钮，这是**导航**（真需要用户动手时才给）。
  // 刻意不加「去设置换模型」：绝大多数失败不是模型问题，每次都挂那颗按钮是噪音。
  return (
    <div className="verdict miss">
      <span className="v-tag">这一轮没跑完</span>
      <div className="v-quote">{node.label || "出了点问题。"}</div>
      <div className="v-hint">你的原文还在，没有丢失。</div>
      {onRetry ? (
        <div className="v-hint" style={{ cursor: "pointer" }} onClick={onRetry}>
          重连 →{" "}
        </div>
      ) : null}
    </div>
  );
}

/* ---------- 时间线 ---------- */
export default function Transcript({
  nodes,
  running,
  onRetry,
  missNotes,
}: {
  nodes: TimelineNode[];
  running: boolean;
  onRetry?: () => void;
  /**
   * 另一协作者在途的「哪几轮没省到」提示（口径来自 `adapters/usage.ts::deriveUsage`）。
   * 它在本次改造前就没有渲染落点，且 AskView 一直在传——**保留入参以免删掉别人的在途功能**，
   * 落点确定后再接（见 docs/design/ask-transcript-render-design.md §11）。
   */
  missNotes?: Map<string, string>;
}) {
  const t = useAgent();
  const setPanelOpen = useStore((s) => s.setPanelOpen);
  // 展开态：按**稳定组 id** 存（toolGroups 用轮次锚点 + 段序号编），只活在本次会话的渲染里，
  // 不进账本；用户手动打开后不会被状态变化自动折回（设计稿 §5）。
  const [openIds, setOpenIds] = useState<Record<string, boolean>>({});
  const toggle = (id: string) => setOpenIds((m) => ({ ...m, [id]: !m[id] }));

  // 思考块「想了多久」：节点 id → 首次见到的时间戳。只活在渲染层，不进账本、不落盘。
  // 一秒一跳的钟**只在还有思考块在跑时开**，跑完立刻停（不空转）。
  const seenAt = useRef<Record<string, number>>({});
  const runningThink = nodes.some((n) => n.kind === "reasoning" && !n.done);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!runningThink) return;
    const timer = setInterval(() => tick((v) => v + 1), 1000);
    return () => clearInterval(timer);
  }, [runningThink]);
  useEffect(() => {
    for (const n of nodes) {
      if (n.kind === "reasoning" && seenAt.current[n.id] === undefined) {
        seenAt.current[n.id] = Date.now();
      }
    }
  }, [nodes]);

  const last = nodes[nodes.length - 1];
  const compacting = !!last && last.kind === "compaction";
  // 决策卡等待用户回答时，卡片自己有「等你选择后继续」，不再叠一行「思考中」
  const askWaiting = !!last && last.kind === "ask" && last.state === "waiting";
  // 思考块自己不叠「思考中」行（它组头已经写了「正在思考…」，两处一起转圈是噪音）
  const reasoningLive = !!last && last.kind === "reasoning";
  const waiting =
    running && !(last && last.kind === "text") && !compacting && !askWaiting && !reasoningLive;

  const units = toRenderUnits(nodes);

  /** 过程块里的一个节点（工具 / 思考 / 旁白）。 */
  const renderProcessNode = (n: TimelineNode): ReactNode => {
                if (n.kind === "tool") {
                  return (
                    <StepRow
                      key={n.id}
                      node={n}
                      open={!!openIds["s-" + n.id]}
                      onToggle={() => toggle("s-" + n.id)}
                    />
                  );
                }
                if (n.kind === "reasoning") {
                  const seconds = n.done
                    ? n.durationMs === undefined
                      ? null
                      : Math.max(0, Math.round(n.durationMs / 1000))
                    : Math.max(
                        0,
                        Math.floor(
                          (Date.now() - (n.startedAt ?? seenAt.current[n.id] ?? Date.now())) /
                            1000,
                        ),
                      );
                  const thinkTitle = n.done
                    ? seconds === null
                      ? "思考"
                      : "已思考 · " + seconds + " 秒"
                    : "思考中 · " + seconds + " 秒";
                  return (
                    <div className="pthink" key={n.id}>
                      <ThinkingBlock
                        title={thinkTitle}
                        open={!!openIds[n.id]}
                        onToggle={() => toggle(n.id)}
                      >
                        {n.text}
                      </ThinkingBlock>
                    </div>
                  );
                }
                if (n.kind === "text") {
                  // 过程里的旁白不是交付：压成灰字引用，也不挂复制按钮
                  return (
                    <div className="pnote" key={n.id}>
                      <Markdown text={n.text} />
                    </div>
                  );
                }
                if (n.kind === "notice") {
                  return (
                    <div className="pnote" key={n.id}>
                      {n.message}
                    </div>
                  );
                }
                return null;
  };


  // 空回合：后端 FinishReason 里没有"空回复"这一档，模型回空消息也走 final_text，
  // 前端只看到一个 done。不给提示的话，用户会以为界面卡住了。
  const emptyTurn = isEmptyTurn(nodes);

  return (
    <>
      {/* 读屏播报：视觉隐藏，状态一变就念出来 —— 键盘 / 读屏用户唯一的线索。
          项目此前一个 aria-live 都没有。空时间线时不念，免得开屏就报「已完成」。 */}
      <div className="sr-only" role="status" aria-live="polite">
        {nodes.length === 0 ? "" : emptyTurn ? "这次没有产生内容" : running ? "正在处理" : "已完成"}
      </div>
      {units.map((u) => {
        if (u.kind === "process") {
          const steps = u.nodes.filter((n) => n.kind === "tool" || n.kind === "reasoning");
          const failed = u.nodes.filter((n) => n.kind === "tool" && n.state === "fail").length;
          const isLive = running && u === units[units.length - 1];
          const title = stepHeaderTitle(steps, failed, isLive);
          // 没有步骤（例如模型直接报错）：不画过程块，否则错误信息会被折进一个空块里看不见。
          const body = <>{u.nodes.map((n) => renderProcessNode(n))}</>;
          // 只有"这一轮已经结束且确实没有步骤"才用扁平容器；
          // 运行中始终用同一个过程块壳，避免第一件工具到达时换壳重建（会闪）。
          if (steps.length === 0 && !isLive) {
            return <div key={u.id} className="proc-flat">{body}</div>;
          }
          return (
            <ProcessBlock
              key={u.id}
              title={title}
              tone={failed > 0 ? "fail" : isLive ? "live" : "done"}
              open={!!openIds[u.id]}
              onToggle={() => toggle(u.id)}
            >
              {body}
            </ProcessBlock>
          );
        }
        if (u.kind === "toolGroup") {
          return (
            <ToolGroupBlock
              key={u.id}
              unit={u}
              open={!!openIds[u.id]}
              onToggle={() => toggle(u.id)}
              onOpenPanel={() => setPanelOpen(true)}
            />
          );
        }

        const n = u.node;
        if (n.kind === "user") {
          return <UserTurn key={n.id} text={n.text} attachments={n.attachments} note={n.note} />;
        }
        if (n.kind === "text") {
          return <AgentText key={n.id} text={n.text} caret={running && !n.confirmed} />;
        }
        if (n.kind === "reasoning") {
          // DSH 式思考块：固定标题 + 左侧箭头 + 展开后原文。
          // 完成后的秒数取真实 durationMs，不能拿“现在 - 页面首见”算，否则会越挂越大。
          const seconds = n.done
            ? n.durationMs === undefined
              ? null
              : Math.max(0, Math.round(n.durationMs / 1000))
            : Math.max(
                0,
                Math.floor(
                  (Date.now() - (n.startedAt ?? seenAt.current[n.id] ?? Date.now())) / 1000,
                ),
              );
          const thinkTitle = n.done
            ? seconds === null
              ? "已思考"
              : `已思考 · ${seconds} 秒`
            : `思考中 · ${seconds} 秒`;
          return (
            <ThinkingBlock
              key={n.id}
              title={thinkTitle}
              open={!!openIds[n.id]}
              onToggle={() => toggle(n.id)}
            >
              {n.text}
              {!n.done ? <span className="caret" /> : null}
            </ThinkingBlock>
          );
        }
        if (n.kind === "tool") {
          // 挂卡片的工具：卡片本身就是结果，不再叠一行工具行（设计稿 §3.2）。
          // 「哪些工具是卡片类」只信 toolGroups 那一份判定，这里不再重复列举工具名。
          if (CARD_ONLY_TOOLS.has(n.name)) {
            if (n.name === "get_quiz" && extractQuiz(n.output)) {
              return <QuizCard key={n.id} output={n.output} />;
            }
            if (n.name === "propose_plan" && extractPlan(n.output)) {
              return <PlanProposalCard key={n.id} output={n.output} />;
            }
            if (n.name === "propose_wordbook_add") {
              const proposal = extractWordbookProposal(n.output);
              if (proposal) {
                return <WordbookProposalCard key={n.id} proposal={proposal} />;
              }
            }
            // 卡片还没出来（运行中 / 失败 / 输出坏）：往下走，退回一行组头，别让界面一片空白
          }
          const id = `g-${n.id}`;
          return (
            <ToolGroupBlock
              key={id}
              unit={{ kind: "toolGroup", id, nodes: [n] }}
              open={!!openIds[id]}
              onToggle={() => toggle(id)}
              onOpenPanel={() => setPanelOpen(true)}
            />
          );
        }
        if (n.kind === "ask") {
          // dismissed（steer/取消/迟到点击）不渲染
          if (n.state === "dismissed") return null;
          // 等待回答的卡移到 AskView 底部固定浮层（不随时间线滚、不被输入框挡）；
          // 时间线里只保留已回答后的收合记录，方便回看
          if (n.state === "waiting") return null;
          return <AskCard key={n.id} node={n} onAnswer={t.submitAskAnswer} />;
        }
        if (n.kind === "notice") {
          // 不加前缀字符标记：`.finrow` 两侧自带发丝线，已经框得住这一行
          // （设计稿 §4.3：状态标记一律走 icons.tsx，不许再撒 `◌ ✓ ✗ ■ ▸` 这类字符）
          return (
            <div key={n.id} className="finrow">
              {n.message}
            </div>
          );
        }
        if (n.kind === "compaction") {
          // 上下文压缩进行中：一行转圈 + 固定话术（照原版，不暴露 L2/L3 等术语）
          return (
            <div key={n.id} className="thinkrow">
              <IconRing />
              {COMPACTING_MESSAGE}
            </div>
          );
        }
        if (n.kind === "finalize") {
          return <FinalizeCard key={n.id} node={n} />;
        }
        return <Terminal key={n.id} node={n} onRetry={onRetry} />;
      })}
      {waiting ? (
        <div className="thinkrow">
          <IconRing />
          思考中
        </div>
      ) : null}
      {emptyTurn ? (
        <div className="finrow turn-empty">
          <span className="te-k">这次没有产生内容</span>
          <span className="te-d">再说一次就行。</span>
        </div>
      ) : null}
    </>
  );
}

/* 供外部读取 store 的占位（保持单文件导入面干净） */
export { useStore };