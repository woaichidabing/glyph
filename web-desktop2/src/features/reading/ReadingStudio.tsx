import { useCallback, useMemo, useState } from "react";
import { useStore } from "../../store";
import {
  createAttempt,
  getAttempt,
  getPassage,
  latestAttempt,
  patchAttempt,
  submitAttempt,
  type AttemptPatch,
} from "./readingAttempts";
import { legacyDraftPayload, readLegacySnapshot } from "./readingAttempts";
import { structureIssue } from "./reading";
import type { PassageDetail, PassageMeta, ReadingAttempt } from "./reading";
import PassageLibrary, { type LibraryAction } from "./PassageLibrary";
import Practice from "./Practice";
import Review from "./Review";

type Screen = "library" | "practice" | "review";

/**
 * 精读台总控：篇章 → 一次作答（draft / pending / submitted）→ 复盘。
 *
 * 三件容易被忽视的事实：
 * - attempt 的真相在后端 `~/.markr/reading/`，界面只是它的一层展示；每答一题就
 *   PATCH 一次，失败要显示出来（握在手里的答案还在，不会因为没存上就消失）。
 * - 「重做」必须显式：默认打开的是最近那一次作答，已交卷的打开就是复盘。
 * - 阶段来自真是的记录：draft 拿不到任何参考答案，UI 也就不可能有现有的对错。
 */
export default function ReadingStudio() {
  const setView = useStore(s => s.setView);

  const [screen, setScreen] = useState<Screen>("library");
  const [detail, setDetail] = useState<PassageDetail | null>(null);
  const [attempt, setAttempt] = useState<ReadingAttempt | null>(null);
  const [error, setError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // 旧版 localStorage 进度：进场读一次，只作为迁移来源，之后不再往 localStorage 写
  const legacy = useMemo(() => readLegacySnapshot(), []);

  const open = useCallback(async (meta: PassageMeta, action: LibraryAction = "auto") => {
    setError("");
    setSaveError("");
    const out = await getPassage(meta.quiz_id);
    if (!out.ok) {
      setError(out.error);
      setScreen("library");
      return;
    }
    const passage = out.data;
    const issue = structureIssue(passage.structure);
    if (issue) {
      setError(issue);
      setScreen("library");
      return;
    }
    setDetail(passage);

    if (action === "redo") {
      const created = await createAttempt({
        quiz_id: passage.quiz_id,
        qtype: passage.qtype,
        exam: passage.exam,
        year: passage.year,
        source: passage.source,
      });
      if (!created.ok) {
        setError(created.error);
        return;
      }
      setAttempt(created.data);
      setScreen("practice");
      return;
    }

    const latest = await latestAttempt(passage.quiz_id);
    if (!latest.ok) {
      setError(latest.error);
      return;
    }
    const existing = latest.attempt;

    if (existing) {
      setAttempt(existing);
      setScreen(existing.stage === "submitted" ? "review" : "practice");
      return;
    }

    // 没有服务器记录时，试着把旧版本机草稿搬过来（旧成绩只有摘要，搬不了也不冒充）
    const migrated = legacy?.progress?.[passage.quiz_id];
    const created = await createAttempt({
      quiz_id: passage.quiz_id,
      qtype: passage.qtype,
      exam: passage.exam,
      year: passage.year,
      source: passage.source,
      ...(legacyDraftPayload(passage.structure, migrated) ?? {}),
    });
    if (!created.ok) {
      setError(created.error);
      return;
    }
    setAttempt(created.data);
    setScreen("practice");
  }, [legacy]);

  /** 本地乐观更新 + 落盘。落盘失败**保留界面上的作答**，只把错误显示出来。 */
  const commit = useCallback(
    async (next: ReadingAttempt, patch: AttemptPatch) => {
      setAttempt(next);
      setSaving(true);
      const res = await patchAttempt(next.attempt_id, patch);
      setSaving(false);
      if (!res.ok) {
        setSaveError(res.error);
        return false;
      }
      setSaveError("");
      setAttempt(res.data);
      return true;
    },
    [],
  );

  const submit = useCallback(async () => {
    if (!attempt) return;
    setSubmitting(true);
    setSaveError("");
    const answers = Object.fromEntries(
      attempt.questions.filter(q => q.answer).map(q => [q.question_id, q.answer]),
    );
    const res = await submitAttempt(attempt.attempt_id, answers);
    setSubmitting(false);
    if (!res.ok) {
      // 判卷失败：作答已经保存在服务端（pending），这里只提示可重试
      setSaveError(res.error);
      const fresh = await getAttempt(attempt.attempt_id);
      if (fresh.ok) setAttempt(fresh.data);
      return;
    }
    setAttempt(res.data);
    setScreen("review");
  }, [attempt]);

  const backToLibrary = useCallback(() => {
    setScreen("library");
    setDetail(null);
    setAttempt(null);
    setError("");
    setSaveError("");
  }, []);

  return (
    <div className="rs-shell">
      {error ? (
        <div className="rs-banner err" role="alert">
          <span>{error}</span>
          <button type="button" className="btn ghost sm" onClick={() => setError("")}>知道了</button>
        </div>
      ) : null}

      {screen === "library" ? (
        <PassageLibrary
          legacySummaryCount={
            legacy?.results ? Object.keys(legacy.results).length : 0
          }
          notice={error}
          onOpen={open}
          onExit={() => setView("library")}
        />
      ) : screen === "practice" && detail && attempt ? (
        <Practice
          detail={detail}
          attempt={attempt}
          saving={saving}
          submitting={submitting}
          saveError={saveError}
          onBack={backToLibrary}
          onPatch={commit}
          onSubmit={submit}
        />
      ) : screen === "review" && detail && attempt ? (
        <Review
          detail={detail}
          attempt={attempt}
          onBack={backToLibrary}
          onRedo={() => void open(detail, "redo")}
        />
      ) : (
        <div className="rs-status">
          <p className="qb-err">{error || "出了点状况，回到题库重试。"}</p>
          <button type="button" className="btn ghost sm" onClick={backToLibrary}>回题库</button>
        </div>
      )}
    </div>
  );
}
