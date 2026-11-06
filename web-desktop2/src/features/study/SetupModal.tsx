import { useEffect, useState } from "react";
import type { Wordbook } from "../../types";
import { useStudy } from "./studyStore";
import { estimate, freeProgress, type StudyMode } from "./studyRepo";

function NumRow({ label, note, value, min, max, step = 5, suffix, onChange }: {
  label: string; note: string; value: number; min: number; max: number;
  step?: number; suffix: string; onChange: (n: number) => void;
}) {
  const clamp = (n: number) => Math.max(min, Math.min(max, n));
  return (
    <div className="stm-row">
      <div className="stm-label"><b>{label}</b><span>{note}</span></div>
      <div className="stm-stepper">
        <button type="button" aria-label={label + "减少"} disabled={value <= min} onClick={() => onChange(clamp(value - step))}>−</button>
        <b>{value}</b><em>{suffix}</em>
        <button type="button" aria-label={label + "增加"} disabled={value >= max} onClick={() => onChange(clamp(value + step))}>+</button>
      </div>
    </div>
  );
}

export function SetupModal({ open, book, onClose }: {
  open: boolean;
  book: Wordbook;
  onClose: () => void;
}) {
  const settings = useStudy((s) => s.settings);
  const patchSettings = useStudy((s) => s.patchSettings);
  const [mode, setMode] = useState<StudyMode>(settings.mode ?? "plan");
  const [dailyNew, setDailyNew] = useState(settings.dailyNew || 30);
  const [dailyReview, setDailyReview] = useState(settings.dailyReview || 60);
  const [groupSize, setGroupSize] = useState(settings.groupSize || 5);
  const [autoSound, setAutoSound] = useState(settings.autoSound === true);

  useEffect(() => {
    if (!open) return;
    setMode(settings.mode ?? "plan");
    setDailyNew(settings.dailyNew || 30);
    setDailyReview(settings.dailyReview || 60);
    setGroupSize(settings.groupSize || 5);
    setAutoSound(settings.autoSound === true);
  }, [open, settings.mode, settings.dailyNew, settings.dailyReview, settings.groupSize, settings.autoSound]);

  if (!open) return null;
  const est = mode === "plan" ? estimate(book.id, dailyNew) : null;
  const free = mode === "free" ? freeProgress() : null;

  const apply = () => {
    const modeChanged = mode !== settings.mode || book.id !== settings.bookId;
    patchSettings({
      bookId: book.id,
      mode,
      dailyNew: mode === "plan" ? dailyNew : settings.dailyNew,
      dailyReview,
      groupSize,
      autoSound,
    });
    // 只有换书 / 换背法才重排今天；改额度、发音开关只影响明天。
    if (modeChanged) useStudy.getState().rebuild();
    onClose();
  };

  return (
    <div className="set-mask open" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="stm-modal stm-setup" role="dialog" aria-label="怎么背">
        <div className="stm-head">
          <div><h2>怎么背</h2><p>随时可以改，明天的队列按新设置重排。</p></div>
          <button type="button" className="stm-x" onClick={onClose} aria-label="关闭">✕</button>
        </div>

        <div className="stm-body">
          <div className="stm-row">
            <div className="stm-label"><b>每日节奏</b><span>按计划每天固定量，或随心背到哪算哪。</span></div>
            <div className="stm-choices">
              <button type="button" className={"stm-choice" + (mode === "plan" ? " on" : "")} onClick={() => setMode("plan")}><i className="stm-radio" /><div><b>按计划</b><span>每天 {dailyNew} 个新词，复习按到期推。</span></div></button>
              <button type="button" className={"stm-choice" + (mode === "free" ? " on" : "")} onClick={() => setMode("free")}><i className="stm-radio" /><div><b>随心背</b><span>不设额度，从上次停下的地方继续。</span></div></button>
            </div>
          </div>

          {mode === "plan" ? (
            <>
              <NumRow label="每天新词" note="不含复习和错词。" value={dailyNew} min={5} max={300} onChange={setDailyNew} suffix="词" />
              <NumRow label="每天复习" note="到期的词由算法排队，这里定今天捞多少。" value={dailyReview} min={10} max={300} step={10} onChange={setDailyReview} suffix="词" />
              <NumRow label="每组几个" note="一组背完就出结果。" value={groupSize} min={2} max={10} step={1} onChange={setGroupSize} suffix="个" />
              {est ? (
                <div className="stm-est"><span>按这个速度，约 {est.days} 天背完</span><b>{est.finishDate}</b></div>
              ) : null}
            </>
          ) : (
            <div className="stm-free">
              <p>不设每日额度，今天想背几个就背几个。停在哪儿都记得住，下次从停下的地方接着走。</p>
              <div className="stm-est"><span>已背会 {free?.done ?? 0} 词</span><b>还剩 {free?.left ?? book.total} 词</b></div>
              <p className="stm-hint">随心背没有“今日已毕”，也不参与连续打卡判定。</p>
            </div>
          )}

          <div className="stm-row">
            <div className="stm-label"><b>自动发音</b><span>进入卡片时自动读一遍。</span></div>
            <button type="button" className={"stm-toggle" + (autoSound ? " on" : "")} onClick={() => setAutoSound((v) => !v)}>
              <i />{autoSound ? "开" : "关"}
            </button>
          </div>
        </div>

        <div className="stm-foot"><span>设置只影响背词，不改计划目标</span><div><button type="button" className="stm-ghost" onClick={onClose}>取消</button><button type="button" className="stm-primary" onClick={apply}>保存</button></div></div>
      </div>
    </div>
  );
}
