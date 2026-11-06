/**
 * DSH 式思考块：固定标题 + 左侧箭头 + 缩进原文。
 * 不用虚线卡片，也不把思考第一行当标题。
 */
import type { ReactNode } from "react";
import { IconChevron } from "./icons";
import { StepIcon } from "../features/ask/stepIcons";

export default function ThinkingBlock({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className={"thinkblock" + (open ? " open" : "")}>
      <button type="button" className="thinkhead" aria-expanded={open} onClick={onToggle}>
        <span className="thinkatom" aria-hidden="true">
          <StepIcon name="atom" size={13} />
        </span>
        <IconChevron className="thinkchev" />
        <span className="thinktitle">{title}</span>
      </button>
      <div className="thinkbody" hidden={!open}>{children}</div>
    </div>
  );
}
