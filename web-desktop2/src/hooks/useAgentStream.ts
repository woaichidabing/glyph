/**
 * 问答页的会话状态机：把后端事件流归约成时间线。
 *
 * 顺序、重试边界、终态这些容易错的事全在 `askTimeline` 里（纯函数、有测试），
 * 这里只管三件 React 自己的事：发起请求、取消请求、切换会话时不让旧请求污染新会话。
 *
 * 三条硬规矩：
 * 1. **一次只跑一个请求**。再发一次会先掐断上一个。
 * 2. **切换/新建会话必须先安全取消在途任务**，只 abort 前端会让后端继续空转烧 key。
 * 3. **晚到的旧请求不许写进新会话**：每次 run / 载入都领一个令牌，令牌对不上就丢弃。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { UsageBaseline } from "../adapters/usage";
import {
  streamAsk,
  StreamIdleTimeoutError,
  type AskPayload,
} from "../adapters/agentStream";
import {
  emptyTimeline,
  finalizeUnconfirmed,
  markCancelled,
  reduce,
  replayToTimeline,
  type AskEvent,
  type Timeline,
} from "../adapters/askTimeline";
import type { Uploaded } from "../adapters/uploadAdapter";
import { answerAskUser } from "../adapters/askUserApi";
import {
  buildSubmission,
  markSubmitting,
  resetSubmitting,
  settleSubmitted,
} from "../adapters/askCard";
import type { AskNode } from "../adapters/askTimeline";
import { deletesCurrent } from "../adapters/askSelection";
import { useStore } from "../store";
import {
  batchDeleteSessionsApi,
  deleteSessionApi,
  getSessionMessagesApi,
  listSessionsApi,
  newSessionApi,
  openSessionApi,
  type SessionSummary,
} from "../adapters/sessionApi";
import {
  getLastSessionId,
  sessionToRestore,
  setLastSessionId,
} from "../adapters/lastSession";

export type Status = "idle" | "running" | "settled";

/** 上下文用量环：tokens 为最后一轮 input（null=模型没回 usage，画空环），window 为分母 */
export type ContextUsage = { tokens: number | null; window: number } | null;

export function useAgentStream() {
  const [timeline, setTimeline] = useState<Timeline>(emptyTimeline);
  const [status, setStatus] = useState<Status>("idle");
  const [online, setOnline] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [contextUsage, setContextUsage] = useState<ContextUsage>(null);
  /**
   * 历史会话的用量合计（打开旧会话时后端回灌）。实时那几轮照旧逐轮累加，
   * 状态条把两者相加 —— 不这么做，刷新一下"本会话省了多少"就空了。
   */
  const [historyUsage, setHistoryUsage] = useState<UsageBaseline | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const onlineRef = useRef(false);
  const runningRef = useRef(false);
  /** 每次 run / 载入会话都领一个号；号对不上的事件一律丢弃 */
  const runToken = useRef(0);
  /** timeline 的同步镜像：事件回调（如提交反问答案）里要读最新卡片状态 */
  const timelineRef = useRef<Timeline>(emptyTimeline());
  useEffect(() => {
    timelineRef.current = timeline;
  }, [timeline]);
  const lastPayload = useRef<AskPayload | null>(null);
  /** 刷新恢复只做一次：后续 refreshSessions（如发完消息）不再触发自动打开 */
  const didRestoreRef = useRef(false);

  const refreshSessions = useCallback(async () => {
    if (!onlineRef.current) {
      setSessions([]);
      return;
    }
    try {
      setSessions(await listSessionsApi());
    } catch {
      setSessions([]);
    } finally {
      setSessionsLoaded(true); // 无论有无会话，拉过一次才能判断要不要恢复
    }
  }, []);

  /* 启动：探活后端；在线则拉会话列表（不自动新建/载入，保持空欢迎页） */
  useEffect(() => {
    let alive = true;
    fetch("/api/tools")
      .then((r) => {
        if (!alive) return;
        onlineRef.current = r.ok;
        setOnline(r.ok);
        if (r.ok) void refreshSessions();
      })
      .catch(() => {
        if (alive) {
          onlineRef.current = false;
          setOnline(false);
        }
      });
    return () => {
      alive = false;
    };
  }, [refreshSessions]);

  /** 首次发送时确保后端有会话（在线新建并切换；离线用本地临时 id） */
  const ensureBackendSession = useCallback(async () => {
    if (sessionIdRef.current) return sessionIdRef.current;
    if (onlineRef.current) {
      const id = await newSessionApi();
      sessionIdRef.current = id;
      setSessionId(id);
      return id;
    }
    const id = `local-${Date.now()}`;
    sessionIdRef.current = id;
    setSessionId(id);
    return id;
  }, []);

  /** 通知后端真正取消；只断前端 fetch 会让服务端继续跑 */
  const cancelBackend = useCallback(() => {
    if (!onlineRef.current) return;
    // 带上自己的会话 id：后端按会话各跑各的（同一会话内才排队），不认 id 的话别人按停止会掐掉你的
    const sid = sessionIdRef.current;
    const url = sid ? `/api/cancel?session_id=${encodeURIComponent(sid)}` : "/api/cancel";
    void fetch(url, { method: "POST" }).catch(() => {});
  }, []);

  /** 掐断在途任务（后端 + 前端），并把时间线收成「已停止」 */
  const abortInFlight = useCallback(() => {
    if (!runningRef.current) return;
    runningRef.current = false;
    runToken.current += 1; // 让还在飞的事件全部作废
    cancelBackend();
    abortRef.current?.abort();
    setTimeline((tl) => markCancelled(tl));
    setStatus("settled");
  }, [cancelBackend]);

  const run = useCallback(
    async (payload: AskPayload, token: number) => {
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;
      lastPayload.current = payload;
      runningRef.current = true;
      setStatus("running");
      try {
        await ensureBackendSession();
      } catch {
        /* 新建失败也继续，streamAsk 会如实报错 */
      }
      // 首次发送刚建好的会话 id 必须显式带给后端：payload 是在 send() 里组装的，
      // 那会儿还没有这个 id。不带的话后端只能落回"当前会话"——这期间要是另一个
      // 标签页插进来问了一句，这句话就会写进别人的本子。
      if (!payload.sessionId && sessionIdRef.current) {
        payload = { ...payload, sessionId: sessionIdRef.current };
        lastPayload.current = payload;
      }

      try {
        for await (const ev of streamAsk(payload, ac.signal)) {
          if (ac.signal.aborted || runToken.current !== token) return;
          if (ev.type === "queued") {
            setNotice(ev.message ?? "前一个任务正在执行，已排队等待…");
            continue;
          }
          if (ev.type === "session_bound") {
            // 服务端说的才算：id 打不开时它开了新的，我们跟着改，
            // 否则会一直拿旧 id 来，每轮都被当成"打不开"又新建一个会话。
            if (ev.sessionId && ev.sessionId !== sessionIdRef.current) {
              sessionIdRef.current = ev.sessionId;
              setSessionId(ev.sessionId);
              if (lastPayload.current) {
                lastPayload.current = { ...lastPayload.current, sessionId: ev.sessionId };
              }
            }
            continue;
          }
          setTimeline((tl) => reduce(tl, ev));
          if (ev.type === "result") {
            // 上下文环：只认最后一轮 input；模型没回 usage 时给 null 画空环，绝不谎报 0
            setContextUsage({
              tokens: typeof ev.contextInputTokens === "number" ? ev.contextInputTokens : null,
              window: typeof ev.contextWindow === "number" ? ev.contextWindow : 0,
            });
          }
          if (ev.type === "result" || ev.type === "error") {
            runningRef.current = false;
            setStatus("settled");
            setNotice(null);
            void refreshSessions();
          }
        }
        if (ac.signal.aborted || runToken.current !== token) return;
        // 流到尾却没收到合法终态：退出等待，如实说未确认，不许假装成功
        setTimeline((tl) => finalizeUnconfirmed(tl));
        runningRef.current = false;
        setStatus("settled");
      } catch (e) {
        if (ac.signal.aborted || runToken.current !== token) return;
        if (e instanceof StreamIdleTimeoutError) {
          // 看门狗：连心跳都长时间没收到，不知道后端跑完没，按"状态未确认"处理并给重试，
          // 既不谎报失败也不假装成功（避免重复发请求 / 重复花额度）。
          setTimeline((tl) => finalizeUnconfirmed(tl));
        } else {
          setTimeline((tl) => reduce(tl, { type: "error", message: (e as Error).message }));
        }
        runningRef.current = false;
        setStatus("settled");
      }
    },
    [ensureBackendSession, refreshSessions],
  );

  const send = useCallback(
    (text: string, images: Uploaded[], memory?: string, note?: string) => {
      if (!text.trim() && images.length === 0) return;
      if (!onlineRef.current) {
        setNotice("暂时连不上，请稍后重试。");
        return;
      }
      const token = ++runToken.current;
      setTimeline((tl) => ({
        ...tl,
        seq: tl.seq + 1,
        nodes: [
          ...tl.nodes,
          {
            id: `u-${tl.seq}`,
            kind: "user",
            text,
            // 界面动作（OCR 台确认批改等）替用户发出的指令：存一行小灰条，不显示蓝色气泡
            note,
            attachments: images.map((i, idx) => ({
              id: `up-${tl.seq}-${idx}`,
              name: i.name,
              kind: i.kind === "file" ? "file" : "image",
              url: i.url,
            })),
            sortKey: tl.seq,
          },
        ],
      }));
      setNotice(null);
      void run(
        {
          text,
          images: images.map((i) => ({ id: i.id, name: i.name, blob: i.blob, kind: i.kind })),
          memory,
          // 认会话：服务端按这个 id 翻本子，避免两个标签页互相写错流水
          sessionId: sessionIdRef.current ?? undefined,
        },
        token,
      );
    },
    [run],
  );

  /** POST + fetch 的流式请求不会自愈，重连必须显式发起 */
  const reconnect = useCallback(() => {
    const last = lastPayload.current;
    if (!last) {
      setNotice(null);
      return;
    }
    void run(last, ++runToken.current);
  }, [run]);

  const stop = useCallback(() => {
    abortInFlight();
  }, [abortInFlight]);

  /**
   * 对 ask_user 决策卡提交答复（点选项或自己打字）。
   * 原来的 SSE 流在后端挂起保活（心跳续命），答案注入后**同一条流**继续吐事件，
   * 所以这里不新建流、不重发消息。
   * - 受理：卡片必须处于 waiting 且未在提交中，置 submitting 禁用按钮防重复；
   * - 后端 ok=false（迟到点击 / 问题对不上 / 已取消）：静默收卡，不弹任何错误；
   * - 网络没送到：退回 waiting 让用户重试，不吞掉他的选择。
   */
  const submitAskAnswer = useCallback(
    async (callId: string, choice: string | null, text: string) => {
      const ask = timelineRef.current.nodes.find(
        (n) => n.kind === "ask" && n.callId === callId,
      ) as AskNode | undefined;
      if (!ask || ask.state !== "waiting" || ask.submitting) return;
      const payload = buildSubmission(ask.question, choice, text);
      if (!payload) return;
      setTimeline((tl) => ({
        ...tl,
        nodes: tl.nodes.map((n) =>
          n.kind === "ask" && n.callId === callId ? markSubmitting(n) : n,
        ),
      }));
      try {
        const ok = await answerAskUser(
          payload.kind === "custom"
            ? { text: payload.text, question: payload.question }
            : { choice: payload.choice, question: payload.question },
        );
        // ok=true 也只保持 submitting：卡片由同一条流随后的 tool_result 正式收成 answered
        setTimeline((tl) => ({
          ...tl,
          nodes: tl.nodes.map((n) =>
            n.kind === "ask" && n.callId === callId ? settleSubmitted(n, ok) : n,
          ),
        }));
      } catch {
        setTimeline((tl) => ({
          ...tl,
          nodes: tl.nodes.map((n) =>
            n.kind === "ask" && n.callId === callId ? resetSubmitting(n) : n,
          ),
        }));
      }
    },
    [],
  );

  /** 切会话时清掉会话级暂存：输入区附件、OCR 确认、批阅拍板（id 按会话序号生成，不跨会话） */
  const resetTransient = useCallback(() => {
    useStore.getState().clearTray();
    useStore.getState().clearConfirmedOcr();
    useStore.getState().clearGradeDecisions();
    useStore.getState().setCanvasMode(false);
  }, []);

  /**
   * 定稿接口成功后，把成果节点立即追加进当前时间线（后端也落了流水，刷新/重放会以
   * 后端回灌为准；这里只负责让界面立刻翻到已定稿，不等刷新）。
   */
  const recordLocalFinalize = useCallback(
    (p: {
      callId: string;
      finalText: string;
      comment: string;
      keptCount: number;
      title?: string;
      marks?: unknown;
      explanations?: import("../adapters/askTimeline").FinalizeExplain[];
    }) => {
      setTimeline((tl) => {
        if (tl.nodes.some((n) => n.kind === "finalize" && n.callId === p.callId)) return tl;
        return {
          ...tl,
          seq: tl.seq + 1,
          nodes: [...tl.nodes, {
            id: `f-${p.callId}`,
            kind: "finalize" as const,
            callId: p.callId,
            finalText: p.finalText,
            comment: p.comment,
            keptCount: p.keptCount,
            title: p.title,
            marks: p.marks,
            explanations: p.explanations,
            sortKey: tl.seq + 1,
          }],
        };
      });
    },
    [],
  );

  /**
   * 新会话：先安全取消在途任务，再回到未开局的空白页。
   * 🔑 **不向后端要新会话**（2026-10-12 人反馈）：点「新对话」只是清空本地视图，
   * 会话推迟到第一次发消息时才真正创建（ensureBackendSession）。之前每点一次就
   * POST /api/sessions/new 落一个没人说过的「未命名会话」，侧栏越堆越多。
   */
  const reset = useCallback(async () => {
    abortInFlight();
    lastPayload.current = null; // 空白页没有"重连上一轮"可言，别把旧会话的消息带进新会话
    sessionIdRef.current = null;
    setSessionId(null);
    setTimeline(emptyTimeline());
    resetTransient();
    setContextUsage(null); // 新会话上下文从零开始
    setHistoryUsage(null); // 用量合计同理：新会话从零开始
    setStatus("idle");
    setNotice(null);
  }, [abortInFlight]);

  /** 从历史栏载入：先取消在途任务，再后端切换并回放重建时间线 */
  const openSession = useCallback(
    async (id: string): Promise<boolean> => {
      if (!onlineRef.current) return false;
      abortInFlight();
      const token = ++runToken.current;
      try {
        await openSessionApi(id);
        const msg = await getSessionMessagesApi(id);
        if (runToken.current !== token) return false; // 期间又切了别的会话：这次结果作废
        setTimeline(replayToTimeline(msg.replay));
        resetTransient();
        sessionIdRef.current = id;
        setSessionId(id);
        // 回显历史用量：token 环的分子用后端回灌的最后一轮 input；分母由 BrainPicker
        // 按当前大脑 + 档位自算（ContextUsage.window 字段暂无消费方，填 0 即可）
        setContextUsage(
          msg.last_input_tokens != null ? { tokens: msg.last_input_tokens, window: 0 } : null,
        );
        setHistoryUsage(
          msg.usage
            ? {
                inputTokens: msg.usage.input_tokens ?? 0,
                outputTokens: msg.usage.output_tokens ?? 0,
                cachedTokens:
                  typeof msg.usage.cached_tokens === "number" ? msg.usage.cached_tokens : null,
              }
            : null,
        );
        setStatus("idle");
        setNotice(null);
        return true;
      } catch (e) {
        if (runToken.current !== token) return false;
        const msg = (e as Error).message;
        const busy = /409|正在执行|收尾/.test(msg);
        // 上一轮还在收尾：如实说"再等一下"，别只是一个转瞬即逝的小黄条
        setNotice(
          busy
            ? "上一轮还在收尾，等一两秒再切（它已经跑完了，结果就在里面）"
            : `打开失败：${msg}`,
        );
        return false;
      }
    },
    [abortInFlight],
  );

  /* DEV 可视化钩子：用一串步骤播种时间线，供 headless 截图各批次界面
     （货架/OCR 核对/批阅/定稿）。步骤可为工具事件，或 {kind:"user"} 乐观用户节点。
     生产构建里整段被 dead-code 消除。 */
  useEffect(() => {
    if (!import.meta.env.DEV) return;
    type SeedStep = AskEvent | {
      kind: "user";
      text?: string;
      images?: { name: string; url?: string; imageId?: string }[];
    };
    (window as unknown as { __seedTimeline?: (steps: SeedStep[]) => void }).__seedTimeline =
      (steps: SeedStep[]) => {
        runToken.current += 1;
        runningRef.current = false;
        let tl = emptyTimeline();
        let evIdx = 0;
        for (const s of steps) {
          if ("kind" in s && s.kind === "user") {
            tl = {
              ...tl,
              seq: tl.seq + 1,
              nodes: [...tl.nodes, {
                id: `u-${tl.seq}`, kind: "user" as const, text: s.text ?? "",
                attachments: (s.images ?? []).map((im, j) => ({
                  id: `up-${tl.seq}-${j}`, name: im.name, kind: "image" as const,
                  url: im.url, imageId: im.imageId,
                })),
                sortKey: tl.seq,
              }],
            };
          } else {
            tl = reduce(tl, s as AskEvent, 1000 + evIdx);
            evIdx += 1;
          }
        }
        setTimeline(tl);
        setStatus("settled");
        setNotice(null);
      };
  }, []);

  /* 记住当前会话，刷新 /ask 后据此恢复（B-T10-1）；变成 null 时清掉指针 */
  useEffect(() => {
    setLastSessionId(sessionId);
  }, [sessionId]);

  /* 首次拉到会话列表后，按指针恢复上次会话；只尝试一次，之后的列表刷新不再触发。
     恢复的是空会话时视觉上仍是欢迎页；指针失效（会话已删）则留在欢迎页。
     后端上一轮还在收尾时 open 会 409：这里退避重试几次，别让用户看到一个
     "点了没反应"的欢迎页（2026-10-18 真机踩到）。 */
  useEffect(() => {
    if (didRestoreRef.current || !sessionsLoaded) return;
    didRestoreRef.current = true;
    const target = sessionToRestore(getLastSessionId(), sessions);
    if (!target) return;
    const tryOpen = async (attempt: number) => {
      const ok = await openSession(target);
      if (!ok && attempt < 6) {
        window.setTimeout(() => void tryOpen(attempt + 1), 800 * (attempt + 1));
      }
    };
    void tryOpen(0);
  }, [sessionsLoaded, sessions, openSession]);

  /** 删除会话；删的是当前会话时，后端返回新建会话 id，前端回到空对话 */
  const removeSession = useCallback(
    async (id: string) => {
      if (!onlineRef.current) return;
      try {
        const r = await deleteSessionApi(id);
        if (sessionIdRef.current === id) {
          abortInFlight();
          sessionIdRef.current = r.new_session_id;
          setSessionId(r.new_session_id);
          setTimeline(emptyTimeline());
          resetTransient();
          setContextUsage(null);
          setStatus("idle");
        }
        await refreshSessions();
      } catch (e) {
        setNotice(`删除失败：${(e as Error).message}`);
      }
    },
    [abortInFlight, refreshSessions],
  );

  /**
   * 批量删除会话。返回真删掉的数量，界面据此决定要不要退出选择模式。
   *
   * 两个不说谎的地方：
   * 1. 后端是逐个删的，可能只成功一部分（有的已被删、有的编号不合法），
   *    所以用 `deleted.length` 而不是请求条数当结果——不能报「已删 3 个」实际只删了 2 个。
   * 2. 删到当前会话时后端会新建一个并切换，前端必须接住这个新 id 并清掉时间线，
   *    否则界面会停在一个已经不存在的会话上。
   */
  const removeSessions = useCallback(
    async (ids: string[]): Promise<number> => {
      if (!onlineRef.current || ids.length === 0) return 0;
      try {
        const r = await batchDeleteSessionsApi(ids);
        const gone = new Set(r.deleted ?? []);
        if (deletesCurrent(Array.from(gone), sessionIdRef.current) || r.new_session_id) {
          abortInFlight();
          sessionIdRef.current = r.new_session_id;
          setSessionId(r.new_session_id);
          setTimeline(emptyTimeline());
          resetTransient();
          setContextUsage(null);
          setStatus("idle");
        }
        await refreshSessions();
        const asked = ids.length;
        const done = gone.size;
        if (done < asked) setNotice(`删掉了 ${done} 个，另外 ${asked - done} 个没能删除`);
        return done;
      } catch (e) {
        setNotice(`批量删除失败：${(e as Error).message}`);
        return 0;
      }
    },
    [abortInFlight, refreshSessions],
  );

  return {
    timeline,
    status,
    running: status === "running",
    online,
    notice,
    sessionId,
    sessions,
    contextUsage,
    historyUsage,
    send,
    reconnect,
    stop,
    submitAskAnswer,
    reset,
    openSession,
    removeSession,
    removeSessions,
    refreshSessions,
    recordLocalFinalize,
    dismissNotice: () => setNotice(null),
  };
}

export type { Timeline, TimelineNode, ToolNode } from "../adapters/askTimeline";
export type { AskEvent };
