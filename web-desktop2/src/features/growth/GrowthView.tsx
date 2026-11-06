import { useEffect, useState } from "react";
import { buildLocalGrowth, loadFinalizeCounts, type GrowthModel } from "./growthRepo";
import { useWordbook } from "../wordbook";

/**
 * 成长页视图模型：查词历史读后端快照（store 的 history + ready），其余本地账本照旧。
 * 快照没到（ready=false）时 model 保持 null，页面显示“正在翻你的账本…”，
 * 不拿空数组冒充“一次都没查过”。后端不可达时定稿数显示 —。
 */
function useGrowth(hidden: boolean) {
  const history = useWordbook((s) => s.history);
  const bookReady = useWordbook((s) => s.ready);
  const [model, setModel] = useState<GrowthModel | null>(null);
  const [countsMissing, setCountsMissing] = useState(false);

  // 进成长页拉一次后端快照（低频页）：没 ready 就 hydrate（内部去重），已 ready 就 refresh。
  // 这样启动时后端没起来、后来起来了，也能在下次进页面时补上，而不是永远显示 0。
  useEffect(() => {
    if (hidden) return;
    const book = useWordbook.getState();
    if (book.ready) void book.refresh();
    else void book.hydrate();
  }, [hidden]);

  useEffect(() => {
    if (hidden || !bookReady) return;
    let cancelled = false;
    const now = new Date();
    // 本地账先画一版（定稿数先按 0），后端计数回来再补
    setModel(buildLocalGrowth(history, { total: 0, thisYear: 0 }, now));
    loadFinalizeCounts(now).then((c) => {
      if (cancelled) return;
      setCountsMissing(c.unavailable);
      setModel(buildLocalGrowth(history, c, now));
    });
    return () => {
      cancelled = true;
    };
  }, [hidden, bookReady, history]);

  return { model, countsMissing };
}

export default function GrowthView({ hidden }: { hidden: boolean }) {
  const [gtab, setGtab] = useState<"ov" | "yr">("ov");
  const { model, countsMissing } = useGrowth(hidden);

  // 四维分数要等批改能力升级（grade 产出四维分）后才有数据；在此之前不放假曲线
  const chartSoon = (
    <div className="g-empty">
      四维能力曲线正在等数据——等你攒够几篇作文批改（内容切题 / 篇章结构 / 语言准确 /
      表达丰富），这里会画出只属于你的四条线。现在不放示例曲线，免得给你假的进步感。
    </div>
  );
  const dimsSoon = (
    <p className="g-meta" style={{ marginTop: 8 }}>
      四维上下半年对比要等批改升级后才有真实分数，届时自动填充。
    </p>
  );

  return (
    <section className="view" id="view-home" hidden={hidden}>
      <div className="gr-wrap">
        <header className="gr-head">
          <div>
            <h2 className="gr-h1">成长</h2>
            <p className="gr-desc">不设币，不设商城。这里只摆一件事：你确实在变好——错词拿下了几个，四条线怎么走的，印章墙上又添了几枚。</p>
          </div>
          <div className="plan-tabs" style={{ marginBottom: 0 }}>
            <button className={"ptab" + (gtab === "ov" ? " on" : "")} onClick={() => setGtab("ov")}>概览</button>
            <button className={"ptab" + (gtab === "yr" ? " on" : "")} onClick={() => setGtab("yr")}>年度档案</button>
          </div>
        </header>

        {!model ? <div className="g-empty">正在翻你的账本…</div> : (
        <div id="gv-ov" hidden={gtab !== "ov"} style={gtab === "ov" ? { display: "flex", flexDirection: "column", gap: 14 } : undefined}>
          <div className="card g-card">
            <div className="g-row">
              <div>
                <div className="g-klabel">已拿下错词</div>
                <div className="g-bignum">{model.mastered.total} <span>个</span><em>本月 +{model.mastered.thisMonth}</em></div>
              </div>
              <p className="g-quote">这是唯一能证明“错误真的被消灭了”的指标：背了多少词、学了多久都只是投入，错词本里的词被移出，才算产出。</p>
            </div>
            <div className="g-chips">
              {model.mastered.words.map(w => <span key={w}>{w}</span>)}
              {model.mastered.more > 0 ? <span className="dim">还有 {model.mastered.more} 个</span> : null}
              {model.mastered.total === 0 ? <span className="dim">还没有——错词本里拿下的词会记在这里</span> : null}
            </div>
          </div>

          <div className="card g-card">
            <div className="g-secrow">
              <div className="g-klabel">四维能力</div>
            </div>
            {chartSoon}
          </div>

          <div className="card g-card">
            <div className="g-klabel">成就墙</div>
            <div className="g-seals">
              {model.achievements.map(a => (
                <div key={a.name} className={"seal" + (a.at ? " on" : "")} title={a.desc}>
                  <b>{a.name}</b><span>{a.desc}</span>
                  <span className="seal-bar"><i style={{ width: Math.min(100, Math.round(a.now / a.need * 100)) + "%" }} /></span>
                  <em>{a.at ?? `${a.now} / ${a.need}`}</em>
                </div>
              ))}
            </div>
            <p className="g-meta">达成的盖实心章，没达成的留虚线空位——空位也写清了条件，是可期待的目标，不是还没画完的格子。</p>
          </div>

          <div className="g-totals">
            {model.totals.map(t => (
              <div key={t.k} className="tstat">
                <div className="g-klabel">{t.k}</div>
                <div className="g-num">{t.k === "改过作文" && countsMissing && t.v === 0 ? "—" : t.v}</div>
              </div>
            ))}
          </div>
          {countsMissing ? (
            <p className="g-meta">「改过作文」暂时读不到，稍后会自动补上。</p>
          ) : null}
        </div>
        )}

        {model ? (
        <div id="gv-yr" hidden={gtab !== "yr"} style={gtab === "yr" ? { display: "block" } : undefined}>
          <div className="yr-sheet">
            <div className="yr-head">
              <div>
                <div className="g-klabel" style={{ color: "#7a8089" }}>{model.year} 年度学习档案</div>
                <h3 className="yr-title" style={{ fontFamily: "'Ma Shan Zheng','KaiTi','楷体',serif" }}>这一年，你走到了这里</h3>
              </div>
              <div className="yr-seal"><span>年度</span><b>{model.year}</b></div>
            </div>
            <div className="yr-stats">
              {model.annual.cells.map(c => (
                <div key={c.k} className="cell"><div className="g-klabel">{c.k}</div>
                  <b>{c.k === "批改篇数" && countsMissing && c.v === 0 ? "—" : c.v}</b></div>
              ))}
            </div>
            <div className="g-klabel" style={{ marginTop: 18, color: "#7a8089" }}>四维变化（上半年 → 下半年）</div>
            {dimsSoon}
            <p className="yr-foot">{model.annual.foot}</p>
          </div>
        </div>
        ) : null}
      </div>
    </section>
  );
}
