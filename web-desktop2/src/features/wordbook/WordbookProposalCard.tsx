import { useState } from "react";
import { useWordbook } from "./wordbookStore";
import type { WordbookProposal } from "./wordbookProposal";

/**
 * 收词确认卡（已定 A：页边批注）。
 *
 * 设计约束（用户 2026-10-20 拍板，同类卡片不再出新版）：
 * - 只在工具确实递回 proposed=true 时出现；未收录 / 已收藏不渲染（Transcript 退回工具行）。
 * - 不弹窗、不加阴影；暖纸 + 暗红左线 + 衬线词头，和词典页同一套纸。
 * - 只有一个实心主按钮“收进生词本”；“先不用”是文字按钮。
 * - 用户点下之前绝不说已收；点下后才把状态切成“✓ 已收进生词本 + 撤销”。
 * - “先不用”只影响这张卡（本地态），可以“重新考虑”反悔。
 */

const norm = (word: string) => word.trim().toLowerCase();

function ipaOf(proposal: WordbookProposal): string {
  const raw = (proposal.phonetic?.uk || proposal.phonetic?.us || "").trim();
  if (!raw) return "";
  return /^\/.*\/$/.test(raw) ? raw : `/${raw}/`;
}

export default function WordbookProposalCard({ proposal }: { proposal: WordbookProposal }) {
  const key = norm(proposal.word);
  const saved = useWordbook((s) => s.items.some((item) => norm(item.word) === key));
  const liveCount = useWordbook((s) => {
    const hit = s.history.find((item) => norm(item.word) === key);
    return hit?.count ?? 0;
  });
  const count = Math.max(liveCount, proposal.lookedUpCount);

  const [skipped, setSkipped] = useState(false);
  const [busy, setBusy] = useState<"save" | "undo" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const why = saved
    ? count > 0
      ? `你查过 ${count} 次，已在生词本里。`
      : "已在生词本里。"
    : count > 0
      ? `你查过 ${count} 次，还没收进生词本。`
      : "还没查过这个词，先收进去也行。";

  async function save() {
    if (busy) return;
    setBusy("save");
    setError(null);
    const store = useWordbook.getState();
    await store.add({
      word: proposal.word,
      phonetic: {
        uk: proposal.phonetic?.uk ?? "",
        us: proposal.phonetic?.us ?? "",
      },
      senses: [{ pos: proposal.pos, translation: proposal.gloss, examples: [] }],
      source: "cache",
    });
    if (!useWordbook.getState().has(proposal.word)) {
      setError("没保存上，再试一次。");
    }
    setBusy(null);
  }

  async function undo() {
    if (busy) return;
    setBusy("undo");
    setError(null);
    await useWordbook.getState().remove(proposal.word);
    if (useWordbook.getState().has(proposal.word)) {
      setError("撤销没成功，再试一次。");
    }
    setBusy(null);
  }

  const ipa = ipaOf(proposal);

  return (
    <article className="wb-card" aria-label={`收词建议：${proposal.word}`}>
      <div className="wb-label">生词本</div>
      <div className="wb-wordrow">
        <h3 className="wb-word">{proposal.word}</h3>
        {ipa ? <span className="wb-ipa">{ipa}</span> : null}
        {proposal.pos ? <span className="wb-pos">{proposal.pos}</span> : null}
      </div>
      <p className="wb-gloss">{proposal.gloss}</p>
      <p className="wb-why">{why}</p>

      {saved ? (
        <div className="wb-saved">
          <span className="wb-check" aria-hidden="true">✓</span>
          <span>已收进生词本</span>
          <button className="wb-text" type="button" onClick={undo} disabled={busy !== null}>
            {busy === "undo" ? "撤销中…" : "撤销"}
          </button>
        </div>
      ) : skipped ? (
        <div className="wb-skipped">
          <span>好，先不收。</span>
          <button className="wb-text" type="button" onClick={() => setSkipped(false)}>
            重新考虑
          </button>
        </div>
      ) : (
        <div className="wb-actions">
          <button className="wb-primary" type="button" onClick={save} disabled={busy !== null}>
            {busy === "save" ? "收进中…" : "收进生词本"}
          </button>
          <button className="wb-text" type="button" onClick={() => setSkipped(true)} disabled={busy !== null}>
            先不用
          </button>
        </div>
      )}

      {error ? <p className="wb-error" role="alert">{error}</p> : null}
    </article>
  );
}
