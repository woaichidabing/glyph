import { useEffect, useMemo, useState } from "react";
import { useStore } from "../store";
import { useAgent } from "../agent/AgentContext";
import { toMs, type SessionSummary } from "../adapters/sessionApi";
import { countSelected, pruneSelection, selectAll, toggleId } from "../adapters/askSelection";

const NAV: { key: "ask" | "dict" | "study" | "plan"; label: string; icon: JSX.Element }[] = [
  { key: "ask", label: "新对话", icon: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
  )},
  { key: "dict", label: "词典", icon: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
  )},
  { key: "study", label: "背词", icon: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>
  )},
  { key: "plan", label: "计划", icon: (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
  )},
];

/* ---------- 会话分组与相对时间（纯展示） ---------- */
function dayStart(ts: number): number {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}
function relTime(at: number): string {
  const min = Math.floor((Date.now() - at) / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const d = new Date(at);
  const p = (n: number) => String(n).padStart(2, "0");
  return hr < 48 ? `昨日 ${p(d.getHours())}:${p(d.getMinutes())}` : `${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
type Group = { label: string; items: SessionSummary[] };
function groupSessions(list: SessionSummary[]): Group[] {
  const today = dayStart(Date.now());
  const yest = today - 86400000;
  const g: Group[] = [
    { label: "今天", items: [] },
    { label: "昨天", items: [] },
    { label: "更早", items: [] },
  ];
  list.forEach((s) => {
    const d = dayStart(toMs(s.created_at));
    (d >= today ? g[0] : d >= yest ? g[1] : g[2]).items.push(s);
  });
  return g.filter((x) => x.items.length > 0);
}

export default function Sidebar() {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const setPanelOpen = useStore((s) => s.setPanelOpen);
  const agent = useAgent();

  // 批量选择 / 删除的界面态（纯前端，不进全局 store）
  const [picking, setPicking] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [confirmMany, setConfirmMany] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const online = agent.online;
  const busy = agent.running; // 任务在跑时后端会拒绝删除，按钮直接不让点
  const sessions = agent.sessions;
  const aliveIds = useMemo(() => sessions.map((s) => s.session_id), [sessions]);

  // 列表刷新 / 删完后，把已不存在的会话从选中集合里剔掉，避免重复发给后端
  useEffect(() => {
    setSelected((cur) => (cur.length ? pruneSelection(cur, aliveIds) : cur));
  }, [aliveIds]);

  const exitPicking = () => {
    setPicking(false);
    setSelected([]);
    setConfirmMany(false);
  };
  const picked = countSelected(selected, aliveIds);
  const allPicked = aliveIds.length > 0 && picked === aliveIds.length;
  const groups = useMemo(() => (online ? groupSessions(sessions) : []), [sessions, online]);

  const goNew = () => {
    setView("ask");
    void agent.reset();
  };
  const open = (id: string) => {
    setConfirmId(null);
    setView("ask");
    void agent.openSession(id);
  };

  return (
    <aside className="sb" aria-label="导航与会话">
      <div className="sb-brand">
        <img className="logo" src="/icon.png" alt="Glyph" width={28} height={28} />
        <span className="sb-name">Glyph</span>
      </div>

      <nav className="sb-nav">
        {NAV.map((n) => (
          <button
            key={n.key}
            className={"sb-item" + (view === n.key ? " on" : "")}
            onClick={() => {
              if (n.key === "ask") goNew();
              else setView(n.key);
            }}
          >
            {n.icon}{n.label}
          </button>
        ))}
      </nav>

      <div className="sb-sec">
        <span>{picking ? (picked ? `已选 ${picked} 个` : "选择会话") : "会 话"}</span>
        {online && sessions.length > 0 ? (
          picking ? (
            <button className="sb-mini" title="完成选择" onClick={exitPicking}>完成</button>
          ) : (
            <button
              className="sb-mini"
              title="管理会话"
              aria-label="管理会话"
              onClick={() => {
                setConfirmId(null);
                setPicking(true);
              }}
            >
              管理
            </button>
          )
        ) : null}
      </div>

      <div className="sb-list">
        {!online ? (
          <div className="sb-empty">暂时连不上，看不到历史会话。</div>
        ) : groups.length === 0 ? (
          <div className="sb-empty">还没有会话，问过一次就会出现在这里。</div>
        ) : null}

        {groups.map((g) => (
          <div key={g.label}>
            <div className="sb-group">{g.label}</div>
            {g.items.map((it) => {
              const id = it.session_id;
              const on = agent.sessionId === id;
              const checked = selected.includes(id);
              const click = () => {
                if (deleting || busy) return;
                if (picking) {
                  setSelected(toggleId(selected, id));
                  setConfirmMany(false);
                  return;
                }
                open(id);
              };
              return (
                <div
                  key={id}
                  className={"rail-item" + (on ? " on" : "") + (picking ? " picking" : "")}
                  role={picking ? "checkbox" : "button"}
                  aria-checked={picking ? checked : undefined}
                  aria-label={picking ? `选择会话：${it.title}` : undefined}
                  tabIndex={0}
                  onClick={click}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      click();
                    }
                  }}
                >
                  {picking ? (
                    <span className={"rail-check" + (checked ? " on" : "")} aria-hidden>{checked ? "✓" : ""}</span>
                  ) : null}
                  <span className="t">{it.title || "（未命名会话）"}</span>

                  {picking ? null : (
                    <>
                      {!busy && (
                        <button
                          type="button"
                          className="rail-more"
                          title="更多操作"
                          aria-label="更多操作"
                          aria-haspopup="dialog"
                          aria-expanded={confirmId === id}
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmId(confirmId === id ? null : id);
                          }}
                        >
                          ···
                        </button>
                      )}
                      {confirmId === id && (
                        <div
                          className="rail-confirm-pop"
                          role="dialog"
                          aria-label="确认删除会话"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <b>删掉「{it.title || "（未命名会话）"}」？</b>
                          <p>删除后不能恢复。</p>
                          <div className="rail-confirm-actions">
                            <button
                              type="button"
                              className="rc-cancel"
                              onClick={(e) => {
                                e.stopPropagation();
                                setConfirmId(null);
                              }}
                            >
                              取消
                            </button>
                            <button
                              type="button"
                              className="rc-ok"
                              onClick={(e) => {
                                e.stopPropagation();
                                void agent.removeSession(id);
                                setConfirmId(null);
                              }}
                            >
                              删除
                            </button>
                          </div>
                        </div>
                      )}
                    </>
                  )}
                  {picking || confirmId === id ? null : <time>{relTime(toMs(it.created_at))}</time>}
                </div>
              );
            })}
          </div>
        ))}
      </div>

      {/* 批量删除操作条：两步确认，破坏性操作不一步到位 */}
      {picking ? (
        <div className="sb-pickbar">
          {confirmMany ? (
            <>
              <span className="pb-txt">{deleting ? `正在删除 ${picked} 个…` : `删掉这 ${picked} 个会话？`}</span>
              <button
                type="button"
                className="pb-ok"
                disabled={deleting}
                onClick={async () => {
                  const ids = pruneSelection(selected, aliveIds);
                  setDeleting(true);
                  try {
                    const done = await agent.removeSessions(ids);
                    if (done > 0) exitPicking();
                    else setConfirmMany(false);
                  } finally {
                    setDeleting(false);
                  }
                }}
              >
                {deleting ? "删除中…" : `确认删 ${picked}`}
              </button>
              <button type="button" className="pb-cancel" disabled={deleting} onClick={() => setConfirmMany(false)}>
                返回
              </button>
            </>
          ) : (
            <>
              <button type="button" className="pb-link" onClick={() => setSelected(allPicked ? [] : selectAll(selected, aliveIds, true))}>
                {allPicked ? "全不选" : "全选"}
              </button>
              <span className="pb-txt" style={{ flex: 1 }}>
                {busy ? "任务进行中，暂不能删除" : picked ? `已选 ${picked} 个` : "点会话勾选"}
              </span>
              <button type="button" className="pb-del" disabled={picked === 0 || busy} onClick={() => setConfirmMany(true)}>
                删除所选
              </button>
            </>
          )}
        </div>
      ) : null}

      <div className="sb-foot">
        {!online ? (
          <>
            <span className="rdot" aria-hidden style={{ background: "rgb(var(--ink) /.35)" }} />
            <span>暂时连不上，稍后重试</span>
          </>
        ) : null}
        <span style={{ flex: 1 }} />
        <button className="sb-mini" onClick={() => setPanelOpen(true)}>设 置</button>
      </div>
    </aside>
  );
}
