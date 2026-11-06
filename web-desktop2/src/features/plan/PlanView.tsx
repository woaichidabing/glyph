import { useMemo, useState } from "react";
import { useStore } from "../../store";
import { DayEditModal, WeekRhythmModal } from "./RhythmEditor";
import { readStreak } from "../../adapters/streakRepo";
import { dayKey, monthMatrix, monthStats, todayList, weekCells } from "../../adapters/planStats";
import type { PlanTask } from "../../adapters/planStats";

const WEEKDAY = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

function timeOf(ms?: number | null): string {
  if (!ms) return "";
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function sourceOf(t: PlanTask): string {
  if (t.id.startsWith("r-")) return "周节奏";
  if (t.src === "own") return "自己加的";
  if (t.kind === "study") return "来自背词";
  if (t.kind === "dict") return "来自查词";
  return "按计划";
}

function noteOf(t: PlanTask, today: string): string {
  if (t.done) return timeOf(t.doneAtMs) ? "已完成 · " + timeOf(t.doneAtMs) : "已完成";
  // 昨天排的、没做完的：顺延显示，并标明是补做的
  if (t.scheduledFor && t.scheduledFor < today) return "昨天 · 补做";
  if (t.scheduledFor && t.scheduledFor > today) return "未到";
  if (t.target && t.target > 1) return "还差 " + Math.max(0, t.target - (t.progress ?? 0)) + " 个";
  return "今天";
}

export default function PlanView({ hidden }: { hidden: boolean }) {
  const { tasks, goal, phases, rhythm, planReady, planError, resetPlan, saveRhythm, savePlanDay, setPlanGoal, addTask, toggleTask, removeTask, restoreTask, setView, setAskDraft } = useStore();
  const [tab, setTab] = useState<"day" | "week" | "month">("day");
  const [quick, setQuick] = useState("");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  /** 刚删掉的任务：给 6 秒撤销窗口（删一条任务弹窗太重） */
  const [undoTask, setUndoTask] = useState<PlanTask | null>(null);
  /** 下方列表正在看哪一天（默认今天）；点周节奏的某列就切过去 */
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [weekEdit, setWeekEdit] = useState(false);
  const [dayEdit, setDayEdit] = useState(false);
  /** 正在编辑的周节奏：哪天 + 草稿（保存前不动真数据） */

  const now = useMemo(() => new Date(), [tasks.length, goal?.text, hidden]);
  const today = dayKey(now);
  // 今天该看到的：今天的任务 + 之前没做完的（顺延），已完成的历史任务不再占位
  const visible = useMemo(
    () =>
      tasks.filter(
        (t) => !t.scheduledFor || t.scheduledFor === today || (!t.done && t.scheduledFor < today),
      ),
    [tasks, today],
  );
  const todayTasks = useMemo(() => todayList(visible, now), [visible, now]);
  const week = useMemo(() => weekCells(tasks, now), [tasks, now]);
  const month = useMemo(() => monthMatrix(tasks, now), [tasks, now]);
  const mstats = useMemo(() => monthStats(tasks, now), [tasks, now]);
  const streak = readStreak();
  const viewingDay = selectedDay ?? today;
  const isViewingToday = viewingDay === today;
  const dayTasks = isViewingToday
    ? todayTasks
    : tasks.filter((t) => t.scheduledFor === viewingDay);
  const dayDone = dayTasks.filter((t) => t.done).length;
  const viewDate = new Date(viewingDay + "T00:00:00");
  const viewTitle = isViewingToday
    ? "今天"
    : "周" + "日一二三四五六"[viewDate.getDay()] + " · " + (viewDate.getMonth() + 1) + " 月 " + viewDate.getDate() + " 日";
  const done = todayTasks.filter(t => t.done).length;
  const next = todayTasks.find(t => !t.done);
  const weekNo = goal ? Math.max(1, Math.floor((now.getTime() - new Date(goal.startDate + "T00:00:00").getTime()) / 604800000) + 1) : 1;
  const dateText = (now.getMonth() + 1) + " 月 " + now.getDate() + " 日";
  const CN = ["零", "一", "两", "三", "四", "五", "六", "七", "八", "九"];
  const dayTitle = todayTasks.length
    ? "今天做" + (CN[todayTasks.length] ?? todayTasks.length) + "件就够了"
    : "今天还没排";
  /** 当前阶段：按周数落在哪个区间 */
  const currentPhase = phases.find((p) => weekNo >= p.fromWeek && weekNo <= p.toWeek) ?? null;
  const weekdayKey = String(((now.getDay() + 6) % 7) + 1); // 周一=1
  const todayRhythm = rhythm[weekdayKey] ?? [];

  const chat = (text: string) => { setAskDraft(text); setView("ask"); };
  const saveGoal = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true); setError("");
    try {
      await setPlanGoal(text);
      chat("我想" + text + "，帮我制定一份能每天执行的计划。");
    } catch (e) {
      setError(e instanceof Error ? e.message : "目标没保存上，再试一次。");
    } finally { setBusy(false); }
  };
  const saveQuick = async () => {
    const text = quick.trim();
    if (!text) return;
    setQuick("");
    try { await addTask(text, viewingDay); } catch (e) { setError(e instanceof Error ? e.message : "任务没保存上，再试一次。"); }
  };

  if (!planReady) return <section className="view pl-view" id="view-plan" hidden={hidden}><div className="pl-loading">正在翻计划本…</div></section>;

  // 只有目标、没有任何内容的"空壳计划"不算计划：显示首次引导，
  // 否则用户会看到一个"今天还没排"的页面，以为删了计划又冒出来了。
  const hollowPlan =
    !!goal && phases.length === 0 && Object.keys(rhythm).length === 0 && tasks.length === 0;
  if (!goal || hollowPlan) return (
    <section className="view pl-view" id="view-plan" hidden={hidden}>
      <div className="pl-first">
        <div className="pl-first-left">
          <div className="pl-first-date"><b>{String(now.getMonth() + 1).padStart(2, "0")}.{String(now.getDate()).padStart(2, "0")}</b><span>{WEEKDAY[now.getDay()]}<br />第 1 天</span></div>
          <div className="pl-eyebrow">计划 / 第一次</div>
          <h1>先定一个目标，<br />不用很完整。</h1>
          <p className="pl-lead">写一句你三个月后想做到的事。Glyph 会把它拆成每天几件小事；你也可以先自己写今天。</p>
        </div>
        <div className="pl-first-right">
          <div className="pl-goal-entry"><label>接下来想做成什么</label>
            <div className="pl-goal-line"><input value={draft} placeholder="你想达成什么？比如：三个月过六级" onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === "Enter") void saveGoal(); }} /><button className="pl-primary" disabled={busy || !draft.trim()} onClick={() => void saveGoal()}>{busy ? "正在记下…" : "开始制定计划"}</button></div>
            <div className="pl-examples"><span>或者：</span>{["三个月过六级", "每天 20 分钟", "读懂一篇外刊"].map(x => <button key={x} type="button" onClick={() => setDraft(x)}>{x}</button>)}</div>
          </div>
          {error || planError ? <p className="pl-error">{error || planError}</p> : null}
          <div className="pl-how"><div><i>01</i><b>你说目标</b><span>一句话就够。</span></div><div><i>02</i><b>Glyph 制定计划</b><span>先排今天开始的一周。</span></div><div><i>03</i><b>你收下</b><span>计划才进这个页面。</span></div></div>
        </div>
      </div>
    </section>
  );

  return (
    <section className="view pl-view" id="view-plan" hidden={hidden}>
      <div className="pl-head"><div className="pl-head-left"><div className="pl-date-meta"><div className="pl-eyebrow">{dateText} · {WEEKDAY[now.getDay()]} · 第 {weekNo} 周</div><h1>{tab === "day" ? (isViewingToday ? dayTitle : viewTitle + "的安排") : tab === "week" ? "这一周，慢慢来" : "这个月，留点空"}</h1><p>计划 / {tab === "day" ? "今天" : tab === "week" ? "本周" : "本月"}</p></div></div><div className="pl-head-right"><div className="pl-seal">第 {weekNo} 周<span>{todayTasks.length ? done + " / " + todayTasks.length : "未排"}</span></div><div className="pl-streak">连续 <b>{streak.current}</b> 天<br />最长 {streak.best} 天</div></div></div>
      <div className="pl-goal-strip"><span className="pl-label">长期目标</span><span className="pl-goal-text">{goal.text}</span><span className="pl-goal-bar"><i style={{ width: Math.min(100, (weekNo / goal.weeks) * 100) + "%" }} /></span><span className="pl-goal-meta">第 {weekNo} / {goal.weeks} 周</span><button type="button" className="pl-link" onClick={() => chat("我想调整一下目标：" + goal.text + "。")}>调整</button><button type="button" className="pl-link pl-link-danger" onClick={() => setConfirmDelete(true)}>删除</button></div>
      <div className="pl-tabs">{(["day", "week", "month"] as const).map(k => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{k === "day" ? "今天" : k === "week" ? "本周" : "本月"}</button>)}</div>
      {tab === "day" && (currentPhase || phases.length > 0) ? (
        <div className="pl-phase">
          <div className="pl-phase-head">
            <span className="pl-label">当前阶段</span>
            <span className="pl-phase-focus">
              {currentPhase
                ? "第 " + currentPhase.fromWeek + "–" + currentPhase.toWeek + " 周 · " + currentPhase.title + (currentPhase.focus ? "，" + currentPhase.focus : "")
                : "这一周不在任何阶段里"}
            </span>
            <button type="button" className="pl-link" onClick={() => chat("我想调整一下计划的阶段划分。")}>调整</button>
          </div>
          <div className="pl-phase-bars" aria-hidden="true">
            {Array.from({ length: goal?.weeks ?? 12 }, (_, i) => {
              const w = i + 1;
              const state = w < weekNo ? "done" : w === weekNo ? "now" : "";
              return <i key={w} className={state} />;
            })}
          </div>
        </div>
      ) : null}

      {tab === "day" && Object.keys(rhythm).length > 0 ? (
        <div className="pl-rhythm">
          <div className="pl-phase-head">
            <span className="pl-label">周节奏</span>
            <span className="pl-hint" style={{ margin: 0 }}>每天按这个自动展开；没做完的顺延到今天</span>
            <button type="button" className="pl-link" onClick={() => setWeekEdit(true)}>调整周节奏</button>
          </div>
          <div className="pl-rgrid">
            {["1", "2", "3", "4", "5", "6", "7"].map((k, i) => {
              const items = rhythm[k] ?? [];
              const dcol = new Date(now);
              dcol.setDate(now.getDate() - ((now.getDay() + 6) % 7) + (Number(k) - 1));
              const colKey = dcol.getFullYear() + "-" + String(dcol.getMonth() + 1).padStart(2, "0") + "-" + String(dcol.getDate()).padStart(2, "0");
              const isToday = colKey === viewingDay;
              return (
                <button
                  type="button"
                  key={k}
                  className={"pl-rcol" + (isToday ? " today" : "")}
                  title="点一下调整这天"
                  onClick={() => {
                    const d = new Date(now);
                    d.setDate(now.getDate() - ((now.getDay() + 6) % 7) + (Number(k) - 1));
                    const pad = (n: number) => String(n).padStart(2, "0");
                    setSelectedDay(d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()));
                  }}
                >
                  <div className="pl-rhead"><span>{"一二三四五六日"[i]}</span>{isToday ? <b>{isViewingToday ? "今天" : "在看"}</b> : null}</div>
                  {items.length === 0 ? (
                    <div className="pl-rest">休息</div>
                  ) : (
                    <ul>{items.map((x, j) => <li key={j}>{x.t}</li>)}</ul>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}

      {tab === "day" ? (
        <div className="pl-grid">
          <div className="pl-main">
            <div className="pl-section-head">
              <h2>{viewTitle}</h2>
              <button type="button" className="pl-link" onClick={() => setDayEdit(true)}>调整这天</button>
              <span>{dayTasks.length ? <><b>{dayDone}</b> / {dayTasks.length}</> : (isViewingToday ? "今天还没排" : "这天还没排")}</span>
            </div>
            {isViewingToday && next ? <div className="pl-focus"><span className="pl-focus-mark" /><span className="pl-label">先做这件</span><b>{next.t}</b><span className="pl-focus-time">{noteOf(next, today)}</span></div> : null}
            <div className="pl-tasks">{dayTasks.map(t => <div key={t.id} className={"pl-task" + (t.done ? " done" : "") + (t.id === next?.id ? " next" : "")}><button
                  className="pl-check"
                  aria-label="标记完成"
                  disabled={!!t.scheduledFor && t.scheduledFor > today}
                  title={t.scheduledFor && t.scheduledFor > today ? "还没到那天" : "标记完成"}
                  onClick={() => void toggleTask(t.id)}
                >{t.done ? "✓" : ""}</button><div className="pl-task-main"><div className="pl-task-title">{t.t}</div><div className="pl-task-sub"><span className="pl-source">{sourceOf(t)}</span><span className="pl-sep" /><span>{noteOf(t, today)}</span></div></div><button
                className="pl-more"
                aria-label="删除任务"
                onClick={() => {
                  setUndoTask(t);
                  void removeTask(t.id);
                  window.setTimeout(() => setUndoTask((cur) => (cur?.id === t.id ? null : cur)), 6000);
                }}
              >×</button></div>)}
              {dayTasks.length === 0 ? <div className="pl-empty"><b>{isViewingToday ? "今天还没排。" : "这天还没排。"}</b><span>{isViewingToday ? "去对话里让 Glyph 按目标排今天；或者先自己加一件。" : "可以在下面直接加一件；周节奏里有的话，到那天会自动生成。"}</span>{isViewingToday ? <button type="button" className="pl-primary" onClick={() => chat("按“" + goal.text + "”帮我排今天的计划。")}>去对话排今天</button> : null}</div> : null}
            </div>
            {isViewingToday && dayTasks.length > 0 && dayDone === dayTasks.length ? <div className="pl-allclear">今天清空。剩下的时间可以休息。</div> : null}
            <div className="pl-add"><span>＋</span><input value={quick} placeholder="自己加一件今天要做的事，回车保存" onChange={e => setQuick(e.target.value)} onKeyDown={e => { if (e.key === "Enter") void saveQuick(); }} /><button type="button" onClick={() => void saveQuick()}>保存</button></div>
            <p className="pl-hint">没排满也没关系。没做完的任务会留到明天，不会消失，也不催你。</p>
            {error || planError ? <p className="pl-error">{error || planError}</p> : null}
          </div>
          <aside className="pl-aside"><h3>本周完成</h3><div className="pl-week">{week.map(c => <div key={c.date} className={"pl-day" + (c.state === "today" ? " today" : "")}><span className="pl-day-name">{c.weekday.replace("周", "")}</span><span className="pl-day-bar"><i style={{ width: (c.done ? Math.min(100, c.done * 20) : 0) + "%" }} /></span><span className="pl-day-n">{c.state === "future" ? "—" : c.state === "today" ? (c.done > 0 ? c.done + " 件 · " : "") + (c.left === 0 ? "全清" : "剩 " + c.left) : c.done + " 件"}</span></div>)}</div><div className="pl-stats"><div><span>本周完成</span><b>{week.reduce((n, c) => n + c.done, 0)}</b></div><div><span>有完成的天</span><b>{week.filter(c => c.done > 0).length}</b></div></div><p className="pl-aside-note"><b>第 {weekNo} 周，别排满。</b><br />空出来的晚上，不补任务。</p></aside>
        </div>
      ) : tab === "week" ? (
        <div className="pl-week-view">{week.map(c => <div key={c.date} className={"pl-week-cell " + c.state}><span>{c.weekday}</span><b>{c.label}</b><i>{c.state === "future" ? "—" : c.state === "today" ? (c.left === 0 ? "全清" : "剩 " + c.left) : c.done + " 件"}</i></div>)}<p className="pl-hint">没做完的任务一直在今天；不用补打卡。</p></div>
      ) : (
        <div><div className="pl-month-head">{["一", "二", "三", "四", "五", "六", "日"].map(x => <span key={x}>{x}</span>)}</div><div className="pl-month">{month.cells.map((c, i) => <span key={i} className={"pl-month-cell " + c.state}>{c.state !== "blank" ? <i>{c.day}</i> : null}{c.state === "done" || c.state === "today" ? <b /> : null}</span>)}</div><div className="pl-month-stats"><span>本月完成 <b>{mstats.done}</b> 件</span><span>有完成的天 <b>{mstats.activeDays}</b> 天</span><span>连续 <b>{streak.current}</b> 天</span></div></div>
      )}
      {undoTask ? (
        <div className="pl-undo" role="status">
          <span>已删除「{undoTask.t.length > 16 ? undoTask.t.slice(0, 16) + "…" : undoTask.t}」</span>
          <button
            type="button"
            className="pl-undo-btn"
            onClick={() => {
              const item = undoTask;
              setUndoTask(null);
              void restoreTask(item);
            }}
          >
            撤销
          </button>
        </div>
      ) : null}

      {weekEdit ? (
        <WeekRhythmModal
          rhythm={rhythm}
          onClose={() => setWeekEdit(false)}
          onSave={async (next) => {
            await saveRhythm(next, true);
            setWeekEdit(false);
          }}
        />
      ) : null}

      {dayEdit ? (
        <DayEditModal
          title={viewTitle}
          initial={tasks.filter((x) => x.scheduledFor === viewingDay).map((x) => x.t)}
          onClose={() => setDayEdit(false)}
          onSave={async (items, scope) => {
            await savePlanDay(viewingDay, items, scope);
            setDayEdit(false);
          }}
        />
      ) : null}

      {confirmDelete ? (
        <div
          className="pl-modal-backdrop"
          role="presentation"
          onClick={(ev) => { if (ev.target === ev.currentTarget) setConfirmDelete(false); }}
        >
          <div className="pl-modal" role="dialog" aria-modal="true" aria-labelledby="pl-del-title">
            <h3 id="pl-del-title">删除这份计划？</h3>
            <p>下面这些会一起删掉：</p>
            <div className="pl-modal-rows">
              <div><span>目标</span><b>{goal?.text ?? "未命名"}</b></div>
              <div><span>阶段</span><b>{phases.length} 个</b></div>
              <div><span>周节奏</span><b>{Object.keys(rhythm).length} 天</b></div>
              <div>
                <span>任务</span>
                <b>{tasks.length} 件（含已完成 {tasks.filter((x) => x.done).length} 件）</b>
              </div>
            </div>
            <p className="pl-modal-hint">删除后不能撤销；想留着可以点取消。</p>
            <div className="pl-modal-actions">
              <button type="button" className="btn" onClick={() => setConfirmDelete(false)}>取消</button>
              <button
                type="button"
                className="btn danger"
                disabled={deleting}
                onClick={() => {
                  setDeleting(true);
                  void resetPlan()
                    .then(() => setConfirmDelete(false))
                    .catch((e: unknown) => setError(e instanceof Error ? e.message : "没删掉，再试一次。"))
                    .finally(() => setDeleting(false));
                }}
              >
                {deleting ? "正在删除…" : "删除计划"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="pl-foot"><span>计划 / 第 {weekNo} 周</span><span>— {String(weekNo).padStart(2, "0")} —</span><span>本地 · 自动保存</span></div>
    </section>
  );
}
