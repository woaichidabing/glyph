import { useWordbook } from "./wordbookStore";

/**
 * 词典页侧栏：生词本预览（一角自己滚）+ 最近查过。
 * 点词回填到查词框并立刻查；完整生词本管理在背词页。
 */
export function WordbookPanel({ onPick, onGoStudy }: {
  onPick: (w: string) => void;
  onGoStudy: () => void;
}) {
  const items = useWordbook((s) => s.items);
  const history = useWordbook((s) => s.history);
  const clearHistory = useWordbook((s) => s.clearHistory);

  return (
    <>
      <div className="wb-panel">
        <div className="wb-ph"><span className="wb-t">生词本</span><span className="wb-n">{items.length} 词</span></div>
        {items.length === 0 ? (
          <p className="wb-empty">生词本尚是空白。查词时点「收进生词本」，它便落到这里。</p>
        ) : (
          <div className="wb-list">
            {items.map((it) => (
              <button key={it.word} type="button" className="wb-row" onClick={() => onPick(it.word)}>
                <span>{it.word}</span><span className="p">{it.phonetic?.uk || "—"}</span>
              </button>
            ))}
          </div>
        )}
        <button type="button" className="btn primary sm" style={{ width: "100%", marginTop: 10 }} onClick={onGoStudy}>
          查看全部 →
        </button>
      </div>

      <div className="wb-hist">
        <div className="wb-ph">
          <span className="wb-t2">最近查过</span>
          {history.length > 0 ? (
            <button type="button" className="wb-clear" onClick={clearHistory}>清掉</button>
          ) : null}
        </div>
        {history.length === 0 ? (
          <p className="wb-empty">尚无记录。</p>
        ) : (
          <div className="hist-chips">
            {history.slice(0, 18).map((h) => (
              <button key={h.word} type="button" onClick={() => onPick(h.word)}>{h.word}</button>
            ))}
          </div>
        )}
      </div>

      <button type="button" className="btn primary" style={{ width: "100%" }} onClick={onGoStudy}>去背词</button>
    </>
  );
}
