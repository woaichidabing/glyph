/**
 * 单块崩溃兜底（BUG-01 的第二道防线）。
 *
 * 某棵子树在渲染期抛错时，React 默认会卸载**整棵**树 → 内容区灰屏（2026-10-12 真机精读
 * 七选五就是这种表现）。这里把错误拦在子树边界：只把这棵子树换成一句人话 + 一个恢复入口，
 * 其余界面照常可用。
 *
 * 边界说明：这是**界面级恢复**，不是"工具重试"——与设计稿 §7.2「工具块不挂重试按钮」
 * 那条规矩不冲突（那条只管对话时间线里的工具组头）。
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode; label?: string };
type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 留痕便于排查：兜底 UI 只给人话，真实错误进控制台
    console.error("界面这一块崩了：", error, info.componentStack);
  }

  private reload = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div role="alert" style={{ padding: "28px 24px", color: "rgb(var(--ink) / .72)" }}>
          <p style={{ margin: "0 0 12px" }}>
            {this.props.label ?? "这块内容出了点问题"}，可以重新打开。
          </p>
          <button type="button" className="btn ghost sm" onClick={this.reload}>
            重新加载这块
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
