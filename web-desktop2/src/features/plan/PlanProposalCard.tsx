/* ============================================================
 * 计划提议卡：propose_plan 工具产出后挂在时间线里。
 * 计划先给用户看，他点「加入我的计划」才落本地计划（src=glyph）；
 * 想调整不用专门按钮——直接在输入框对 Glyph 说，它会重新规划。
 * ============================================================ */
import { useMemo, useState } from "react";
import { useStore } from "../../store";
import { extractPlan } from "./planCard";

export default function PlanProposalCard({ output }: { output: unknown }) {
  const plan = useMemo(() => extractPlan(output), [output]);
  const acceptPlan = useStore((s) => s.acceptPlan);
  const existingGoal = useStore((s) => s.goal);
  const existingTasks = useStore((s) => s.tasks);
  const existingRhythm = useStore((s) => s.rhythm);
  const setView = useStore((s) => s.setView);
  const [phase, setPhase] = useState<"ask" | "confirm" | "added" | "declined">("ask");
  // 只有目标、没有任务的"空壳计划"不算已有计划（否则会提示覆盖，用户明明删过）
  const hasExisting =
    existingTasks.length > 0 || Object.keys(existingRhythm ?? {}).length > 0;
  const hasRhythm = Object.keys(plan?.rhythm ?? {}).length > 0;
  const rhythmDays = ["1", "2", "3", "4", "5", "6", "7"].map((k) => ({
    label: "一二三四五六日"[Number(k) - 1],
    items: plan?.rhythm?.[k] ?? [],
  }));
  const [error, setError] = useState("");

  if (!plan || phase === "declined") return null;

  if (phase === "added") {
    return (
      <div className="pp-card">
        <div className="pp-done">
          <span className="pp-check">✓</span>
          {hasRhythm ? "计划已加入：按周节奏每天自动生成当天任务。" : `已把 ${plan.tasks.length} 项加入你的计划。`}去「计划」页看看。
          <button type="button" className="pp-go" onClick={() => setView("plan")}>
            查看计划
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="pp-card">
      <div className="pp-head">
        <span className="pp-badge">划</span>
        <span className="pp-title">Glyph 给你规划了一份计划</span>
      </div>
      <div className="pp-goal">
        <span className="pp-goal-label">目标</span>
        <span className="pp-goal-text">{plan.goal}</span>
      </div>
      {hasRhythm ? (
        <ul className="pp-tasks">
          {rhythmDays.map((d) => (
            <li key={d.label} className="pp-task">
              <span className="pp-no">{d.label}</span>
              <span className="pp-ttext">
                {d.items.length ? d.items.map((x) => x.t).join("、") : "休息"}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <ol className="pp-tasks">
          {plan.tasks.map((t, i) => (
            <li key={i} className="pp-task">
              <span className="pp-no">{i + 1}</span>
              <span className="pp-ttext">{t}</span>
            </li>
          ))}
        </ol>
      )}
      <div className="pp-foot">
        <button
          type="button"
          className="pp-accept"
          onClick={() => {
            setError("");
            if (hasExisting && phase !== "confirm") {
              setPhase("confirm");
              return;
            }
            // 有周节奏就别再把"每周清单"塞成今天的任务——今天的事由节奏展开
            void acceptPlan(
              plan.goal,
              hasRhythm ? [] : plan.tasks,
              plan.phases,
              plan.rhythm,
              hasExisting,
            )
              .then(() => setPhase("added"))
              .catch((err: unknown) => {
                setError(err instanceof Error ? err.message : "计划没保存上，再试一次。");
              });
          }}
        >
          {phase === "confirm" ? "覆盖原计划" : "加入我的计划"}
        </button>
        <button type="button" className="pp-decline" onClick={() => setPhase("declined")}>
          先不要
        </button>
        <span className="pp-tip">
          {error ||
            (phase === "confirm"
              ? `计划页里已经有一份（${existingGoal?.text ?? "未命名目标"}），加入这份会覆盖它`
              : "想调整？直接在下面告诉 Glyph")}
        </span>
      </div>
    </div>
  );
}
