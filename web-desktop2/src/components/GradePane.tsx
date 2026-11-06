import { useState } from "react";
import { useAgent } from "../agent/AgentContext";
import { useStore } from "../store";
import type { BatchItem } from "../adapters/batch";
import { buildFinalizePayload } from "../adapters/finalize";
import { finalizeGrade, getRecheckQuota, recheckDetection, type RecheckResult } from "../adapters/gradeApi";
import {
  buildInlineEssay,
  detectionsWithVerdicts,
  recheckOptions,
  type GradeDecision,
  type InlineCard,
  type InlineSegment,
} from "../adapters/inlineGrade";
import ScoreCard from "./ScoreCard";
import { extractErrorsFromFinalize } from "../adapters/errorExtract";
import { addError } from "../features/study";
import { todayStr } from "../adapters/streakRepo";

/* ---------------- 正文段：原文 + 内联痕迹（React 渲染，不用 dangerouslySetInnerHTML） ---------------- */

function EssaySegments({ segments }: { segments: InlineSegment[] }) {
  return (
    <div className="il-paper">
      {segments.map((s, i) => {
        if (s.kind === "plain") return <span key={i}>{s.text}</span>;
        if (s.kind === "pending") {
          return (
            <span key={i} className="il-pending">
              {s.text}<sup className="il-num">{s.index}</sup>
            </span>
          );
        }
        return (
          <span key={i} className="il-accepted">
            <span className="il-old">{s.oldText}</span>
            <span className="il-new">{s.newText}</span>
          </span>
        );
      })}
    </div>
  );
}

/* ---------------- 单条卡：采纳 / 我来改 / 台内驳回 ---------------- */

function DetectionCard({ item, card, quotaRemaining }: {
  item: BatchItem;
  card: InlineCard;
  /** 本条还能再驳回几次；null=后端不可达不限制；0=按钮禁用 */
  quotaRemaining: number | null;
}) {
  const callId = item.gradeCallId!;
  const setDetection = useStore(s => s.setDetection);
  const clearDetection = useStore(s => s.clearDetection);
  const setVerdict = useStore(s => s.setRecheckVerdict);
  const [editing, setEditing] = useState<"none" | "custom" | "dismiss">("none");
  const [draft, setDraft] = useState("");
  const [checking, setChecking] = useState(false);
  const [checkErr, setCheckErr] = useState<string | null>(null);

  const id = card.id;
  const decision = card.decision;
  const action = decision?.action;
  const { effective } = card;
  const isError = card.det.tier === "error";
  const noId = !id || id.startsWith("det-");
  const gate = recheckOptions(card, quotaRemaining, checkErr !== null);

  const submitDecision = (d: GradeDecision) => {
    if (!id) return;
    setDetection(callId, id, d);
    setEditing("none");
  };

  const runRecheck = async () => {
    if (!item.grade || checking) return;
    setChecking(true);
    setCheckErr(null);
    try {
      const res = await recheckDetection({
        text: item.grade.text,
        // 复核针对的是这条「当前生效」的版本（改判后用的是新 corrected）
        detection: {
          ...card.det,
          corrected: effective.corrected,
          explanation_zh: effective.explanation_zh,
          tier: effective.tier,
          type: effective.type,
        },
        note: draft.trim(),
      });
      const r: RecheckResult = res.recheck;
      useStore.getState().setRecheckQuota(res.quota);
      // 复核结论回来：清掉这条之前的表态，维持/改判都要重新拍板
      setVerdict(callId, id, {
        status: r.status,
        round: r.round,
        corrected: r.corrected,
        explanation_zh: r.explanation_zh,
        tier: r.tier,
        type: r.type,
        reason_zh: r.reason_zh,
        note_zh: r.note_zh,
      });
      clearDetection(callId, id);
      setEditing("none");
    } catch (e) {
      setCheckErr(e instanceof Error ? e.message : "复核没跑成，稍后再试");
      // 409/网络失败时响应里不带配额：补拉一次，让「次数用完」立刻刷成可保持原文
      try {
        useStore.getState().setRecheckQuota(await getRecheckQuota());
      } catch {
        /* 配额只是辅助显示，拉不到不挡保持原文出口 */
      }
    } finally {
      setChecking(false);
    }
  };

  /* 复核撤销的折叠态 */
  if (card.recheckStatus === "withdrawn") {
    return (
      <div className="ncard withdrawn">
        <div className="done-line">
          <span className="tag gray">已撤销</span>
          <span className="muted">第 {card.index} 条（{effective.original}）复核后认为不算错</span>
          <span className="sp" />
          <button className="op link" onClick={() => setVerdict(callId, id, undefined)}>恢复</button>
        </div>
      </div>
    );
  }

  const verdict = card.recheckStatus === "upheld" || card.recheckStatus === "revised" ? card : null;
  const adopted = action === "accepted" || action === "edited";

  return (
    <div className="ncard" data-kind={isError ? "bad" : "sugg"} data-done={action ? (adopted ? "adopt" : "dismiss") : "open"}>
      <div className="n-top">
        <span className={"tag " + (isError ? "red" : "yellow")}>{isError ? "要改" : "建议"}</span>
        {card.det.type && card.det.type !== "hard" ? <span className="tag gray">{card.det.type}</span> : null}
        {card.recheckRound ? <span className="tag gray">复核第 {card.recheckRound} 次</span> : null}
        {!card.inline ? <span className="tag gray" title="这条在原文里没定位到，只在下面列出">位置不确定</span> : null}
        <span className="n-idx">{card.index}</span>
      </div>

      <div className="n-fix">
        <del>{effective.original}</del>
        {effective.corrected ? <> → <ins>{effective.corrected}</ins></> : null}
        {action === "edited" ? <span className="mine-tag">你的版本</span> : null}
      </div>

      {editing === "custom" ? (
        <div className="n-custom">
          <input
            autoFocus
            value={draft}
            placeholder="把它改成你的版本"
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === "Enter" && draft.trim()) submitDecision({ action: "edited", override: draft.trim() });
            }}
          />
          <button className="op link" onClick={() => setEditing("none")}>取消</button>
          <button
            className="op adopt"
            disabled={!draft.trim()}
            onClick={() => draft.trim() && submitDecision({ action: "edited", override: draft.trim() })}
          >
            用我的
          </button>
        </div>
      ) : null}

      {editing === "dismiss" ? (
        <div className="n-dismiss">
          <input
            autoFocus
            value={draft}
            placeholder="补一句理由（可不填）"
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") void runRecheck(); }}
          />
          <button className="op link" onClick={() => { setEditing("none"); setDraft(""); }}>取消</button>
          <button className="op adopt" disabled={checking} onClick={() => void runRecheck()}>
            {checking ? "核对中…" : "重新核对"}
          </button>
        </div>
      ) : null}

      <div className="n-why">{effective.explanation_zh || "（这条没有给出讲解）"}</div>

      {verdict?.recheckStatus ? (
        <div className={"recheck-banner " + verdict.recheckStatus}>
          <b>{verdict.recheckStatus === "upheld" ? "维持原判" : "改判"}</b>
          {card.recheckReason ? <span>：{card.recheckReason}</span> : null}
          <span className="muted">
            {verdict.recheckStatus === "revised"
              ? " —— 采纳新建议，或保持原文。"
              : " —— 采纳改法，或坚持保持原文。"}
          </span>
        </div>
      ) : null}
      {checkErr ? <div className="recheck-err">{checkErr}</div> : null}

      {checking ? (
        <div className="n-ops checking"><span className="spin" /><span className="muted">正在重新核对这一条…</span></div>
      ) : adopted ? (
        <div className="n-ops">
          <button className="op done-tag">✓ {action === "edited" ? "已按你的改法" : "已采纳"}</button>
          <button className="op link" onClick={() => id && clearDetection(callId, id)}>
            撤销
          </button>
        </div>
      ) : action === "rejected" ? (
        <div className="n-ops">
          <button className="op done-tag" style={{ color: "rgb(var(--ink) /.45)" }}>已驳回</button>
          <button className="op link" onClick={() => id && clearDetection(callId, id)}>
            撤销
          </button>
        </div>
      ) : (
        <div className="n-ops">
          <button className="op adopt" disabled={noId} onClick={() => submitDecision({ action: "accepted" })}>
            {card.recheckStatus === "revised" ? "采纳新建议" : "采纳"}
          </button>
          <button className="op" disabled={noId} onClick={() => {
            setEditing("custom");
            setDraft(decision?.override ?? effective.corrected ?? "");
          }}>我来改</button>
          {gate.canAskModel ? (
            <button className="op" disabled={noId} onClick={() => { setEditing("dismiss"); setDraft(""); }}>
              {gate.recheckLabel}
            </button>
          ) : null}
          {gate.canKeepOriginal ? (
            <button
              className="op link"
              disabled={noId}
              title="不再调模型，按你的意见保留原文"
              onClick={() => submitDecision({ action: "rejected" })}
            >
              保持原文
            </button>
          ) : null}
        </div>
      )}
    </div>
  );
}

/* ---------------- 批阅台：内联改稿 + 看完了结账 ---------------- */

export default function GradePane({ item, index }: { item: BatchItem; index: number }) {
  const t = useAgent();
  const callId = item.gradeCallId;
  const decisions = useStore(s => (callId ? s.gradeDecisions[callId] : undefined)) ?? {};
  const rechecks = useStore(s => (callId ? s.gradeRechecks[callId] : undefined)) ?? {};
  const quota = useStore(s => s.recheckQuota);
  const adoptAllDetections = useStore(s => s.adoptAllDetections);
  const clearGradeDecisions = useStore(s => s.clearGradeDecisions);
  const clearGradeRechecks = useStore(s => s.clearGradeRechecks);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState<string | null>(null);
  const [copyNote, setCopyNote] = useState<string | null>(null);

  const grade = item.grade;
  if (!grade || !callId) return null;
  const dets = grade.detections ?? [];

  const essay = buildInlineEssay(grade.text, dets, decisions, rechecks);
  // 用卡片的 id（缺 id 时已兜底成 det-N），不要另算一份：算两份就会出现
  // “按钮禁用、卡片却在”的不一致。
  const ids = essay.cards.map(c => c.id);
  // 定稿请求体带「改判后」的字段：后端只认请求体 detections，不合并会与界面漂移
  const effectiveDets = detectionsWithVerdicts(dets, rechecks);
  const payload = buildFinalizePayload({ text: grade.text, detections: effectiveDets, decisions });
  const stats = grade.stats ?? {};

  const copyChanged = async () => {
    try {
      await navigator.clipboard.writeText(essay.changedText);
      setCopyNote("已复制改后版");
      setTimeout(() => setCopyNote(null), 2000);
    } catch {
      setCopyNote("复制失败，请手动选择文本复制");
      setTimeout(() => setCopyNote(null), 3000);
    }
  };

  const finish = async () => {
    if (submitting) return;
    setSubmitting(true);
    setErr(null);
    try {
      const res = await finalizeGrade({
        ...payload,
        session_id: t.sessionId,
        grade_call_id: callId,
      });
      t.recordLocalFinalize({
        callId,
        finalText: res.final_text,
        comment: payload.comment,
        keptCount: res.kept.length,
        title: item.title,
        marks: res.marks,
        explanations: res.kept.map(d => ({
          original: d.original,
          corrected: d.corrected,
          explanationZh: d.explanation_zh,
          tier: d.tier,
        })),
      });
      clearGradeDecisions(callId);
      clearGradeRechecks(callId);
      // 首次结账落账后把词级硬错收进错词本（只在 appended 时做，一篇只收一次）
      if (res.finalize_record === "appended") {
        const extracted = extractErrorsFromFinalize({
          kept: res.kept,
          sourceText: grade.text,
          today: todayStr(),
        });
        extracted.forEach(e =>
          addError({ word: e.word, from: e.from, sentence: e.sentence, reason: e.reason }),
        );
        setSavedNote(
          extracted.length > 0
            ? `已把 ${extracted.length} 个写错的词收进错词本。`
            : "这次没有词级硬错，不用往错词本里收。",
        );
      }
      if (res.warnings.length > 0) console.warn("finalize warnings:", res.warnings);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "没完成，请再试一次");
      setSubmitting(false);
    }
  };

  const quotaRemainingFor = (card: InlineCard): number | null => {
    if (!quota) return null;
    if (card.id in quota.per_item_remaining) return quota.per_item_remaining[card.id];
    return Math.max(0, quota.per_item_max - (card.recheckRound ?? 0));
  };

  return (
    <div className="pane grade-pane" style={{ display: "flex", flexDirection: "column", minHeight: 0, flex: 1 }}>
      <div className="wb-head">
        <h2>批改 · 第 {index + 1} 篇 · {item.title}</h2>
        <span style={{ flex: 1 }} />
      </div>

      <div className="wb-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {grade.summary ? <div className="wb-gist">{grade.summary}</div> : null}
        <ScoreCard scores={grade.scores} />
        <div className="wb-statline">
          {stats.words != null ? <span>{stats.words} 词</span> : null}
          {stats.error_count ? <span>·</span> : null}
          {stats.error_count ? <span style={{ color: "var(--sem-red)" }}>{stats.error_count} 处要改</span> : null}
          {stats.suggestion_count ? <span>·</span> : null}
          {stats.suggestion_count ? <span style={{ color: "var(--sem-yellow-ink)" }}>{stats.suggestion_count} 处建议</span> : null}
          <span style={{ flex: 1 }} />
          <span>已处理 {essay.progress.decided}/{essay.progress.total}</span>
        </div>

        {/* 内联改稿正文：采纳即时套入，驳回/撤销的位置保持原文 */}
        <EssaySegments segments={essay.segments} />

        {dets.length === 0 ? (
          <div className="wb-gist">这篇没挑出需要改的地方，写得不错。</div>
        ) : (
          <div className="w-notes">
            {essay.cards.map(card => (
              <DetectionCard
                key={card.id}
                item={item}
                card={card}
                quotaRemaining={quotaRemainingFor(card)}
              />
            ))}
          </div>
        )}
      </div>

      <div className="wb-foot">
        {err ? <span style={{ color: "var(--sem-red)", fontSize: 12 }}>{err}</span> : null}
        {savedNote ? <span style={{ color: "var(--sem-green)", fontSize: 12 }}>{savedNote}</span> : null}
        {copyNote ? <span style={{ color: "var(--sem-green)", fontSize: 12 }}>{copyNote}</span> : null}
        <span style={{ flex: 1 }} />
        <button
          className="btn sm"
          disabled={submitting || ids.length === 0}
          title={ids.length === 0 ? "这篇没有可采纳的条目" : "把所有条目都标成采纳"}
          onClick={() => {
            adoptAllDetections(callId, ids);
            // 点了必须看得见结果：即使一条都没变，也给一句反馈
            setSavedNote("已全部采纳");
            setTimeout(() => setSavedNote(null), 2000);
          }}
        >
          全部采纳
        </button>
        <button className="btn sm" disabled={submitting} onClick={() => void copyChanged()}>复制改后版</button>
        <button className="btn primary sm" disabled={submitting || !essay.allDecided}
          title={essay.allDecided ? "" : "每条都得有归宿（采纳 / 我来改 / 驳回）"}
          onClick={() => void finish()}>
          {submitting ? "正在收尾…" : "看完了"}
        </button>
      </div>
    </div>
  );
}
