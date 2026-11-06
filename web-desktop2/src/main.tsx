import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { hydrateAppearanceFromServer } from "./store";
import { AgentProvider } from "./agent/AgentContext";
import "./styles/tokens.css";
import "./styles/base.css";
import "./features/ask/ask.css";
import "./features/ask/thinking.css";
import "./features/dict/dict.css";
import "./features/plan/plan.css";
import "./features/plan/plan-v2.css";
import "./styles/archive.css";
import "./styles/grading.css";
import "./styles/terminal.css";
import "./features/settings/settings.css";
import "./features/settings/storage.css";
import "./features/settings/appearance.css";
import "./features/dict/dict-legacy.css";
import "./features/study/study.css";
import "./styles/archive2.css";
import "./features/settings/settings-panel.css";
import "./styles/responsive.css"

// 桌面端（Electron）铺满整个窗口：去掉浏览器里那种四周留白 + 圆角外壳
if (typeof window !== "undefined" && window.glyphDesktop) {
  document.documentElement.dataset.desktop = "1";
};
import "./styles/slice8.css";
import "./features/study/study-cards.css";
import "./features/study/study-v2.css";
import "./features/study/study-modals-v3.css";
import "./features/ask/ask-card.css";
import "./styles/score-card.css";
import "./features/quiz/quiz.css";
import "./features/settings/slots.css";
import "./styles/misc.css";
// 今天新加的三块界面（状态条 / 用量面板 / 上下文面板）单开一个文件：
// 原先写在 template.css 里，被并发写入覆盖过一次，分开就不会再被误伤。
import "./styles/usage.css";
import "./features/dict/dict-page.css";
import "./features/wordbook/wordbook-card.css";

// 先按本地设置渲染，再向后端补读一次（桌面端换端口也不会丢主题）
void hydrateAppearanceFromServer();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AgentProvider>
      <App />
    </AgentProvider>
  </React.StrictMode>,
);
