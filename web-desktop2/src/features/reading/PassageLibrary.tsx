import { useCallback, useEffect, useMemo, useState } from "react";
import { listAttempts, listPassages } from "./readingAttempts";
import { VIEW_TITLE, type PassageMeta } from "./reading";

const RC_ICON = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="m9 14 2 2 4-4" /></svg>
);
const MATCH_ICON = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M8 3 4 7l4 4" /><path d="M4 7h16" /><path d="m16 21 4-4-4-4" /><path d="M20 17H4" /></svg>
);
const GAP_ICON = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9h6M7 13h4M15 15h2" /></svg>
);

export type LibraryAction = "auto" | "redo" | "review";

type AttemptRow = {
  attempt_id: string;
  stage: string;
  updated_at: string | null;
  submitted_at: string | null;
  results: { correct: boolean; your_answer: string | null }[];
};

/** 题型 → 视图：和后端 structure 的登记保持一致（这里只用于图标和分组名） */
function viewOf(qtype: string): string {
  if (qtype === "段落匹配") return "matching";
  if (qtype === "七选五") return "gap";
  return "choice";
}

function groupTitle(p: PassageMeta): string {
  const src = p.source ?? "";
  const yearPart = p.year && !src.includes(p.year) ? `${p.year}年` : "";
  return [p.exam, yearPart, src].filter(Boolean).join(" · ") || "未分组";
}

export default function PassageLibrary({
  onOpen,
  onExit,
  legacySummaryCount = 0,
  notice = "",
}: {
  onOpen: (meta: PassageMeta, action?: LibraryAction) => void;
  onExit: () => void;
  legacySummaryCount?: number;
  notice?: string;
}) {
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const [passages, setPassages] = useState<PassageMeta[]>([]);
  const [attempts, setAttempts] = useState<Record<string, AttemptRow>>({});
  const [errorMsg, setErrorMsg] = useState("");
  const [view, setView] = useState<"all" | "choice" | "matching" | "gap">("all");
  const [status, setStatus] = useState<"all" | "todo" | "done">("all");
  const [toast, setToast] = useState("");

  const load = useCallback(async () => {
    setState("loading");
    const out = await listPassages();
    if (!out.ok) {
      setErrorMsg(out.error);
      setState("error");
      return;
    }
    setPassages(out.data.passages);
    const all = await listAttempts();
    if (all.ok) {
      const byQuiz: Record<string, AttemptRow> = {};
      // 同一篇可能有很多次作答：取最近一次作为首页状态（历史记录另行可查）
      for (const a of all.data) {
        const prev = byQuiz[a.quiz_id ?? ""];
        byQuiz[a.quiz_id ?? ""] = prev && (prev.updated_at ?? "") > (a.updated_at ?? "")
          ? prev
          : (a as unknown as AttemptRow);
      }
      setAttempts(byQuiz);
    }
    setState("ok");
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const showToast = (m: string) => {
    setToast(m);
    window.setTimeout(() => setToast(""), 2200);
  };

  const groups = useMemo(() => {
    const order: string[] = [];
    const map = new Map<string, PassageMeta[]>();
    for (const p of passages) {
      const t = groupTitle(p);
      const arr = map.get(t);
      if (arr) arr.push(p);
      else {
        map.set(t, [p]);
        order.push(t);
      }
    }
    return order.map(t => ({ title: t, items: map.get(t)! }));
  }, [passages]);

  const filtered = useMemo(
    () => passages.filter(p => {
      const okView = view === "all" || viewOf(p.qtype) === view;
      const st = attempts[p.quiz_id]?.stage;
      const done = st === "submitted";
      const okStatus = status === "all" || (status === "done" ? done : !done);
      return okView && okStatus;
    }),
    [passages, view, status, attempts],
  );

  const counts = useMemo(() => {
    const doing = passages.filter(p => {
      const st = attempts[p.quiz_id]?.stage;
      return st === "draft" || st === "pending_submission";
    }).length;
    const done = passages.filter(p => attempts[p.quiz_id]?.stage === "submitted").length;
    return { doing, done };
  }, [passages, attempts]);

  return (
    <div className="rs-workspace">
      <header className="rs-head">
        <button className="rs-back" onClick={onExit}>← 对话</button>
        <div>
          <div className="rs-htitle">题库 · 精读台</div>
          <div className="rs-hsub">做完才开口讲；一次作答一份记录，重做另外存</div>
        </div>
        <div className="rs-hsp" />
        <button className="btn ghost sm" onClick={() => {
          const pool = passages.filter(p => !attempts[p.quiz_id]);
          if (!pool.length) return showToast("题库里还没有没做过的篇章。");
          onOpen(pool[Math.floor(Math.random() * pool.length)]);
        }}>随机抽一篇</button>
      </header>

      {notice ? <div className="rs-banner err" role="alert">{notice}</div> : null}

      {legacySummaryCount > 0 ? (
        <div className="rs-banner note" role="status">
          检测到 {legacySummaryCount} 条旧版本地成绩（只有最高分和错题号）。那只是摘要，
          <b>原始作答没法凭它恢复</b>；旧版进行中的草稿会自动并入新的作答记录。
        </div>
      ) : null}

      {state === "loading" ? (
        <div className="qb-status"><span className="spin" />正在打开题库…</div>
      ) : state === "error" ? (
        <div className="qb-status">
          <p className="qb-err">{errorMsg}</p>
          <button type="button" className="btn ghost sm" onClick={load}>重试</button>
        </div>
      ) : passages.length === 0 ? (
        <div className="qb-empty">
          <div className="qb-hero">{RC_ICON}</div>
          <h2>还没有可精读的篇章</h2>
          <p className="qb-lead">先去「题库」导入一套真题（仔细阅读 / 长篇阅读 / 七选五），就能在这里逐篇精读。</p>
        </div>
      ) : (
        <div className="rs-body">
          <div className="rs-quick">
            <button className="rs-qcard primary" onClick={() => {
              const doing = passages.filter(p => {
                const st = attempts[p.quiz_id]?.stage;
                return st === "draft" || st === "pending_submission";
              });
              if (!doing.length) return showToast("没有进行中的篇章，先从下面挑一篇开始。");
              onOpen(doing[0]);
            }}>
              <div className="qc-k">继续上次</div>
              <div className="qc-t">{counts.doing ? `${counts.doing} 篇进行中` : "暂无进行中"}</div>
              <div className="qc-d">接着上次没做完的那篇继续</div>
            </button>
            <button className="rs-qcard" onClick={() => {
              const wrongs = passages.filter(p => (attempts[p.quiz_id]?.results ?? []).some(r => !r.correct));
              if (!wrongs.length) return showToast("还没有错题，先做完一篇。");
              onOpen(wrongs[0]);
            }}>
              <div className="qc-k">看上次复盘</div>
              <div className="qc-t">{counts.done ? `${counts.done} 篇已交卷` : "暂无复盘"}</div>
              <div className="qc-d">打开最近一次交卷的结果</div>
            </button>
            <button className="rs-qcard" onClick={() => setStatus("todo")}>
              <div className="qc-k">挑没做过的</div>
              <div className="qc-t">{passages.length - counts.done - counts.doing} 篇没开始</div>
              <div className="qc-d">按题型筛，从没做过的开始</div>
            </button>
          </div>

          <div className="rs-filters">
            <span className="rs-flab">题型</span>
            <span className="rs-seg">
              <button className={view === "all" ? "on" : ""} onClick={() => setView("all")}>全部</button>
              <button className={view === "choice" ? "on" : ""} onClick={() => setView("choice")}>仔细阅读</button>
              <button className={view === "matching" ? "on" : ""} onClick={() => setView("matching")}>长篇阅读</button>
              <button className={view === "gap" ? "on" : ""} onClick={() => setView("gap")}>七选五</button>
            </span>
            <span className="rs-flab">状态</span>
            <span className="rs-seg">
              <button className={status === "all" ? "on" : ""} onClick={() => setStatus("all")}>全部</button>
              <button className={status === "todo" ? "on" : ""} onClick={() => setStatus("todo")}>没做完</button>
              <button className={status === "done" ? "on" : ""} onClick={() => setStatus("done")}>已交卷</button>
            </span>
            <span className="rs-count">共 {passages.length} 篇 · 已交卷 {counts.done} · 进行中 {counts.doing}</span>
          </div>

          {groups.map(g => {
            const visible = g.items.filter(p => filtered.includes(p));
            if (!visible.length) return null;
            return (
              <div className="rs-group" key={g.title}>
                <div className="rs-gtitle">{g.title}</div>
                {visible.map(p => {
                  const row = attempts[p.quiz_id];
                  const stage = row?.stage;
                  const done = stage === "submitted";
                  const right = (row?.results ?? []).filter(r => r.correct).length;
                  const total = row?.results?.length ?? 0;
                  return (
                    <div className="rs-prow" key={p.quiz_id}>
                      <span className="rs-ricon">
                        {viewOf(p.qtype) === "matching" ? MATCH_ICON : viewOf(p.qtype) === "gap" ? GAP_ICON : RC_ICON}
                      </span>
                      <span className="rs-rmain">
                        <div className="rs-rname">{VIEW_TITLE[viewOf(p.qtype)]} · {p.question_count} 题</div>
                        <div className="rs-rmeta">
                          {done && total
                            ? `上次 ${right}/${total}`
                            : stage === "pending_submission"
                              ? "上次交卷没判成，这次继续（作答都还在）"
                              : stage
                                ? "进行中，进度已自动保存"
                                : "未开始"}
                        </div>
                      </span>
                      <span className={"rs-st " + (done ? "done" : stage ? "doing" : "todo")}>
                        {done ? "已交卷" : stage ? "进行中" : "未开始"}
                      </span>
                      <span className="rs-hsp" />
                      <span className="rs-acts">
                        <button className="go" onClick={() => onOpen(p)}>
                          {done ? "查看复盘" : stage ? "继续" : "开始"}
                        </button>
                        {stage ? (
                          <button className="go ghost" onClick={() => onOpen(p, "redo")}>重做</button>
                        ) : null}
                      </span>
                    </div>
                  );
                })}
              </div>
            );
          })}

          <div className="rs-tip">
            ① 一次作答 = 一条记录：重做会新开一条，原来的那份还能翻出来看。<br />
            ② 做题阶段 Glyph 不插话，也不给答案；点「我做完了」才判卷。<br />
            ③ 限时是你自己的训练设置，不是官方统一时限；到点自动按当前作答交卷。
          </div>
        </div>
      )}

      {toast ? <div className="toast show">{toast}</div> : null}
    </div>
  );
}
