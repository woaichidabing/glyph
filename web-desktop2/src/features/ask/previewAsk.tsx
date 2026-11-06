/** 真组件预览：喂假时间线给 Transcript，用来肉眼验收“过程块”长什么样（不进产品）。 */
import { createRoot } from "react-dom/client";
import Transcript from "./Transcript";
import { AgentProvider } from "../../agent/AgentContext";
import type { TimelineNode } from "../../adapters/askTimeline";
import "../../styles/tokens.css";
import "../../styles/base.css";
import "./ask.css";
import "./thinking.css";

const now = Date.now();
const mk = (): TimelineNode[] => {
  let seq = 0;
  const next = () => (seq += 1);
  return [
    { id: "u1", kind: "user", text: "帮我批改这段英文，逐条说清错在哪：", attachments: [], sortKey: next() },
    { id: "r1", kind: "reasoning", text: "用户要批改，先确认不是闲聊。这轮要走 ocr_image，别自己读图，否则会顺手把错字改对。", done: true, durationMs: 1200, startedAt: now - 10000, sortKey: next() },
    { id: "t1", kind: "tool", callId: "c1", name: "ocr_image", state: "ok", startedAt: now - 8800, durationMs: 1800, output: { text: "Last weekend I go to the park with my friend." }, sortKey: next() },
    { id: "x1", kind: "text", text: "我先认一下图里的字。", confirmed: true, sortKey: next() },
    { id: "t2", kind: "tool", callId: "c2", name: "lookup_word", state: "ok", startedAt: now - 6900, durationMs: 300, output: { word: "weekend", phonetic: "ˈwiːkend", definitions: ["n. 周末", "v. 过周末"] }, sortKey: next() },
    { id: "t3", kind: "tool", callId: "c3", name: "grade_text", state: "ok", startedAt: now - 6500, durationMs: 4200, output: { detections: [{ type: "hard", original: "I go", corrected: "I went" }, { type: "hard", original: "We was", corrected: "We were" }, { type: "soft", original: "very like", corrected: "really like" }, { type: "hard", original: "in 2020 year", corrected: "in 2020" }, { type: "hard", original: "the weather is", corrected: "the weather was" }, { type: "soft", original: "good for our health", corrected: "beneficial to our health" }], summary: "整体读得懂，主要问题时态不一致。" }, sortKey: next() },
    { id: "t4", kind: "tool", callId: "c4", name: "remember", state: "ok", startedAt: now - 2200, durationMs: 200, output: { kind: "preference" }, sortKey: next() },
    { id: "t5", kind: "tool", callId: "c5", name: "read_file", state: "fail", startedAt: now - 1900, durationMs: 400, error: "文件不存在：notes.md", sortKey: next() },
    { id: "a1", kind: "text", text: "批完了，6 处，在右边处理。", confirmed: true, sortKey: next() },
  ];
};

function Section({ title, hint, nodes, clicks }: { title: string; hint: string; nodes: TimelineNode[]; clicks: string[] }) {
  return (
    <section className="pv-sec" data-clicks={clicks.join(",")}>
      <h2>{title}</h2>
      <p className="pv-hint">{hint}</p>
      <Transcript nodes={nodes} running={false} />
    </section>
  );
}

createRoot(document.getElementById("root")!).render(
  <AgentProvider>
    <Section title="① 默认：过程折成一行" hint="平时看到的就是这样，只有最终回答露在外面。" nodes={mk()} clicks={[]} />
    <Section title="② 点开过程" hint="每一步一行：图标 + 动词 + 参数 + 右侧信息。" nodes={mk()} clicks={["proc"]} />
    <Section title="③ 再点开一步（看结果）+ 展开思考" hint="结果框、思考原文都在这一步。" nodes={mk()} clicks={["proc", "think", "step-output"]} />
  </AgentProvider>,
);

// 预览用：按 data-clicks 依次点开（每次点完等 React 重渲染）
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await wait(500);
  const secs = Array.from(document.querySelectorAll<HTMLElement>(".pv-sec"));
  for (const sec of secs) {
    const clicks = (sec.dataset.clicks || "").split(",").filter(Boolean);
    for (const c of clicks) {
      const sel = c === "proc" ? ".proc-head" : c === "think" ? ".thinkhead" : ".pstep-head";
      const nodes = sec.querySelectorAll<HTMLElement>(sel);
      const target = c === "step-output" ? nodes[1] ?? nodes[0] : nodes[0];
      target?.click();
      await wait(250);
    }
  }
})();
