/**
 * 状态图标单点：**全部描边 SVG，一个都不许再用字符**。
 *
 * 为什么不用 `✓` `✗` `■` `▸` 这类字符：它们是**字体里的字形**，Windows / macOS / 不同字体
 * 渲染出的粗细、曲率、留白都不同——同一个页面里会有的圆有的尖，这是「不精致」的根因。
 * 换成内联 SVG 后形状由代码定，不受字体影响；描边风格（细线、圆头、扁平）也与现有
 * 设计语言一致。依据：`docs/design/ask-transcript-render-design.md` §4。
 *
 * 颜色一律走 `currentColor`：由使用处的 CSS 变量给色（`--sem-green` / `--sem-red` /
 * `--accent` / `rgb(var(--ink) /.4)`），亮暗主题自动适配。
 */

import type { ReactNode } from "react";
import type { ToolState } from "../adapters/askTimeline";

type IconProps = {
  /** 边长（px）。默认 15——与组头字号 12.5px 并排时的视觉平衡点。 */
  size?: number;
  className?: string;
};

/** 统一的描边规格：2.1px 圆头，24 网格。改粗细只改这里。 */
function Svg({ size = 15, className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.1}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** 成功 / 已完成 */
export function IconCheck(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M20 6.6 9.3 17.3 4.2 12.2" />
    </Svg>
  );
}

/** 失败 */
export function IconCross(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M17.6 6.4 6.4 17.6M6.4 6.4l11.2 11.2" />
    </Svg>
  );
}

/** 已停止：两条竖线（暂停符）——比实心方块柔和，语义也准 */
export function IconStop(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9.8 6.6v10.8M14.2 6.6v10.8" />
    </Svg>
  );
}

/** 可展开：右向折线，展开时由 CSS 旋转 90° */
export function IconChevron(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9.4 5.8 15.6 12l-6.2 6.2" />
    </Svg>
  );
}

/** 进行中：一淡一浓两段弧组成的圆环，旋转交给 CSS（类名 `ringspin`） */
export function IconRing(props: IconProps) {
  const { className, ...rest } = props;
  return (
    <Svg {...rest} className={["ringspin", className].filter(Boolean).join(" ")}>
      <circle cx="12" cy="12" r="8.2" strokeOpacity={0.2} />
      <path d="M12 3.8a8.2 8.2 0 0 1 8.2 8.2" />
    </Svg>
  );
}

/**
 * 按工具状态取图标。**所有渲染状态的地方都走这里**，别在别处再分一遍 if。
 * 颜色不在这里定——由使用处的类给（`.ok` 绿 / `.bad` 红 / `.stop` 中性灰）。
 */
export function StateIcon({ state, ...rest }: IconProps & { state: ToolState }) {
  if (state === "running") return <IconRing {...rest} />;
  if (state === "ok") return <IconCheck {...rest} />;
  if (state === "fail") return <IconCross {...rest} />;
  return <IconStop {...rest} />;
}