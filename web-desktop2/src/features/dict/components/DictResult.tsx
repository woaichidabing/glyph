import type { ReactNode } from "react";
import type { DictEntry } from "../../../types";
import { Pronounce } from "../../../components/Pronounce";

/** 例句里把查询词标出来（大小写不敏感），拆成 [前文, 高亮, 后文…] */
function highlight(text: string, word: string): ReactNode[] {
  const w = word.trim();
  if (!w) return [text];
  const escaped = w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`(${escaped})`, "gi");
  const whole = new RegExp(`^${escaped}$`, "i");
  return text.split(re).map((seg, i) =>
    whole.test(seg) ? <b key={i}>{seg}</b> : <span key={i}>{seg}</span>,
  );
}

const SOURCE_NOTE: Record<DictEntry["source"], string> = {
  api: "本地词库",
  mini: "内置基础词库，词量有限、结果仅供参考",
  cache: "本地缓存",
};

export function DictResult({
  entry,
  query,
  saved,
  onToggleSave,
  onAsk,
}: {
  entry: DictEntry;
  query: string;
  saved?: boolean;
  onToggleSave?: () => void;
  onAsk?: (word: string) => void;
}) {
  const word = query || entry.word;

  return (
    <article className="entry">
      <div className="e-head">
        <h2>{entry.word}</h2>
        <Pronounce word={entry.word} phonetic={entry.phonetic} />
        <span style={{ flex: 1 }} />
        {onToggleSave ? (
          <button
            type="button"
            className={"e-save" + (saved ? " on" : "")}
            onClick={onToggleSave}
            aria-pressed={saved}
          >
            {saved ? "✓ 已收进生词本" : "＋ 收进生词本"}
          </button>
        ) : null}
      </div>

      {entry.queried_form || entry.base_form ? (
        <p className="e-formnote">
          {entry.queried_form
            ? `「${entry.queried_form}」是「${entry.word}」的变形`
            : `「${entry.word}」是「${entry.base_form}」的变形`}
        </p>
      ) : null}

      <div className="e-senses">
        {entry.senses.map((s, i) => (
          <div key={`${s.pos}-${i}`} className="sense-block">
            <div className="sense">
              {s.pos ? <i className="pos">{s.pos}</i> : null}
              <span>{s.translation}</span>
            </div>
            {s.examples.length > 0 ? (
              <div className="e-ex">
                {s.examples.slice(0, 2).map((ex, j) => (
                  <div key={j}>
                    <p className="ex-en">{highlight(ex.en, word)}</p>
                    <p className="ex-cn">{ex.zh}</p>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>

      {entry.forms?.length ? (
        <div className="e-forms">
          {entry.forms.map((f) => <span key={f} className="f">{f}</span>)}
        </div>
      ) : null}

      {entry.tags?.length ? (
        <div className="e-tags">
          {entry.tags.map((t) => <span key={t} className="f">{t}</span>)}
        </div>
      ) : null}

      {onAsk ? (
        <div className="e-askrow">
          <button type="button" className="e-ask-cta" onClick={() => onAsk(entry.word)}>
            <span className="e-ask-logo">M</span>
            问 Glyph · 这个词怎么用
            <span className="e-ask-arrow">→</span>
          </button>
        </div>
      ) : null}

      <div className="e-source">来源：{SOURCE_NOTE[entry.source]}</div>
    </article>
  );
}
