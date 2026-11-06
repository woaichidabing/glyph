/**
 * 折叠组头：**思考块（虚线）与工具块（实线）共用的一套壳**。
 *
 * 为什么做成受控组件（`open` / `onToggle` 由上层给）：
 * 展开态要按**稳定的组 id** 存（见 `adapters/toolGroups.ts` 的 `g-<首节点 id>`），
 * 且「用户手动打开后不许被自动折回」。这份状态属于时间线渲染层，不属于这个壳——
 * 组件自己存 state 会在节点变动导致重挂载时丢掉。
 *
 * 无障碍是白送的：组头是**真 `<button>`**，`aria-expanded` 跟着开合走，
 * Tab 能聚焦、Enter / Space 能开合。右侧的「导航」（如「去设置换模型 →」）刻意放在
 * 按钮**外面**——不嵌套交互元素，也不必靠 stopPropagation 去防误触。
 *
 * 依据：`docs/design/ask-transcript-render-design.md` §2.2 / §2.3 / §5 / §7。
 */

import type { ReactNode } from "react";
import { IconChevron } from "./icons";

/** 语义色。颜色只在 CSS 里定，这里只给身份。 */
export type CollapseTone = "run" | "ok" | "bad" | "stop";

export default function CollapseGroup({
  variant,
  tone,
  icon,
  title,
  action,
  open,
  onToggle,
  children,
}: {
  /** 思考块虚线、工具块实线——两者一眼分得开 */
  variant: "think" | "tool";
  tone: CollapseTone;
  /** 状态图标：一律来自 `components/icons.tsx`，别在这里分 if */
  icon: ReactNode;
  /** 组头文案：工具块走 `adapters/toolPhrases.ts` 拼装 */
  title: string;
  /** 右侧导航。**只在真需要用户动手时**给，且从不是「重试」（设计稿 §7.2） */
  action?: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className={`cgrp ${variant} tone-${tone}${open ? " open" : ""}`}>
      <div className="cgrow">
        <button
          type="button"
          className="cghead"
          aria-expanded={open}
          onClick={onToggle}
        >
          <span className="cgi">{icon}</span>
          <span className="cgttl">{title}</span>
          <IconChevron className="cgch" />
        </button>
        {action ? <span className="cgact">{action}</span> : null}
      </div>
      <div className="cgbody">{children}</div>
    </div>
  );
}