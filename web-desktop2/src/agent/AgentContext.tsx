import { createContext, useContext, type ReactNode } from "react";
import { useAgentStream } from "../hooks/useAgentStream";
import { useBrain } from "../features/settings";

type AgentStream = ReturnType<typeof useAgentStream>;

/** 会话状态机 + 大脑选择（模型 / 推理档 / 上下文档位），全局各一份单例 */
export type Agent = AgentStream & { brain: ReturnType<typeof useBrain> };

const AgentContext = createContext<Agent | null>(null);

/**
 * 全局只调用一次 useAgentStream（它本身约束「一次只跑一个请求」，天然该是单例），
 * 大脑选择 useBrain 也只在这里调一次：在线后拉模型清单 / 当前配置并按需同步后端。
 * 放在 App 外层，让侧栏、对话页、大脑选择器、顶栏共享同一份真数据，
 * 而不是各自调一份、互相看不见对方的状态。
 * 纯界面态（当前视图 / 皮肤 / 草稿 / 工作台开关）仍归 zustand，两类状态分开。
 */
export function AgentProvider({ children }: { children: ReactNode }) {
  const agent = useAgentStream();
  const brain = useBrain(agent.online);
  return <AgentContext.Provider value={{ ...agent, brain }}>{children}</AgentContext.Provider>;
}

/** 取全局会话 + 大脑状态；必须在 <AgentProvider> 内调用 */
export function useAgent(): Agent {
  const ctx = useContext(AgentContext);
  if (!ctx) throw new Error("useAgent 必须在 <AgentProvider> 内部使用");
  return ctx;
}
