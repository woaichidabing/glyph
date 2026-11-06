import { DIMENSIONS, averageScore, scoreBand, type ScoresDTO } from "../adapters/scoreCard";

/**
 * 写作四维评分卡（内容 / 结构 / 准确 / 丰富）。
 * 挂在批阅台总评下方；模型没给分（scores 为空）时整块不渲染。
 * 不适用的维度（如单句谈不到结构）显示「—」，不硬凑分。
 */
export default function ScoreCard({ scores }: { scores: ScoresDTO | null | undefined }) {
  if (!scores) return null;
  const overall = averageScore(scores);

  return (
    <div className="sc-box">
      <div className="sc-head">
        <span className="sc-title">四维评分</span>
        {overall !== null ? (
          <span className="sc-overall" data-band={scoreBand(overall)}>
            综合 {overall}
            <small>/100</small>
          </span>
        ) : null}
      </div>

      <div className="sc-grid">
        {DIMENSIONS.map((d) => {
          const dim = scores[d.key];
          const s = dim.score;
          return (
            <div className="sc-cell" key={d.key} data-band={scoreBand(s)}>
              <div className="sc-cell-top">
                <span className="sc-label">{d.label}</span>
                <span className="sc-num">{s === null ? "—" : s}</span>
              </div>
              <div className="sc-bar">
                <i style={{ width: s === null ? "0%" : `${s}%` }} />
              </div>
            </div>
          );
        })}
      </div>

      <ul className="sc-notes">
        {DIMENSIONS.map((d) => {
          const comment = scores[d.key].comment_zh;
          if (!comment) return null;
          return (
            <li key={d.key}>
              <b>{d.label}</b>
              <span>{comment}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
