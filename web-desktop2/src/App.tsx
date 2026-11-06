import { useEffect } from "react";
import { useStore, applyAppearance } from "./store";
import { useAgent } from "./agent/AgentContext";
import { useWordbook } from "./features/wordbook";
import Sidebar from "./components/Sidebar";
import { Settings } from "./features/settings";
import { AskView } from "./features/ask";
import { DictView } from "./features/dict";
import { StudyView, useStudy } from "./features/study";
import { PlanView } from "./features/plan";
import FirstRunWizard from "./features/onboarding/FirstRunWizard";

const TITLES: Record<string, string> = {
  ask: "Glyph", dict: "词典", study: "背词", plan: "计划",
};

export default function App() {
  const view = useStore(s => s.view);
  const sbHidden = useStore(s => s.sbHidden);
  const toggleSb = useStore(s => s.toggleSb);
  const panelOpen = useStore(s => s.panelOpen);
  const setPanelOpen = useStore(s => s.setPanelOpen);
  const appearance = useStore(s => s.appearance);
  const agent = useAgent();

  useEffect(() => { applyAppearance(appearance); }, [appearance]);

  // 走查用：?settings=1 直接打开设置弹窗（仅 dev；与 ?ctx=1 / ?tab= 同一惯例）
  useEffect(() => {
    if (import.meta.env.DEV && new URLSearchParams(location.search).has("settings")) {
      useStore.getState().setPanelOpen(true);
    }
  }, []);

  // 生词本 / 查词历史在后端：开应用拉一次，成长页和背词页也能直接用。
  useEffect(() => { void useWordbook.getState().hydrate(); }, []);
  // 计划目标 / 任务在后端：开应用拉一次，计划页和对话工具共用同一份。
  useEffect(() => { void useStore.getState().hydratePlan(); }, []);
  // 背词状态在后端：远端有就恢复，没有就把本机旧数据推上去。
  useEffect(() => { void useStudy.getState().hydrateRemote(); }, []);

  return (
    <>
      <div className={"shell" + (sbHidden ? " sb-hidden" : "") + (view === "dict" ? " dict-mode" : "")}>
        <Sidebar />

        <section className="mp">
          {view !== "dict" ? (
            <header className="mp-head">
              <button className="icon-btn" title={sbHidden ? "展开侧栏" : "收起侧栏"}
                aria-expanded={!sbHidden} onClick={toggleSb}>
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>
              </button>
              <h1>{TITLES[view]}</h1>
              <span style={{ flex: 1 }} />
              {view === "ask" && !agent.online ? (
                <span className="chip" role="status">
                  <span style={{
                    width: 7, height: 7, borderRadius: "50%",
                    background: "rgb(var(--ink) /.35)",
                  }} />
                  暂时连不上，稍后重试
                </span>
              ) : null}
            </header>
          ) : null}

          <div className="mp-body">
            <AskView hidden={view !== "ask"} onOpenSettings={() => setPanelOpen(true)} />
            <DictView hidden={view !== "dict"} />
            <StudyView hidden={view !== "study"} />
            <PlanView hidden={view !== "plan"} />
          </div>
        </section>
      </div>

      <Settings open={panelOpen} onClose={() => setPanelOpen(false)} brain={agent.brain} />
      <FirstRunWizard onDone={() => void agent.brain.refresh()} />
    </>
  );
}
