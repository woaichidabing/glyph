"""本轮附件的上下文片段：告诉模型「这轮用户上传了哪些图片、image_id 是什么」。

只陈述事实，不替用户决定要做什么——用户消息里一个字的约定都不加，
意图判断归模型（能推断就办，推断不出用 ask_user 问）。
"""

from collections.abc import Callable

from .inject import ContextFragment


def render_attachments_context(items: list[dict[str, str]] | None) -> str | None:
    """把本轮附件渲染成一段事实；没有附件时返回 None（不注入空壳）。"""
    if not items:
        return None
    lines = [
        "<uploaded_files>",
        "本轮用户上传了以下图片（桌面端报告的事实，不是用户原话）：",
    ]
    for item in items:
        name = (item.get("filename") or "").strip() or "未命名"
        image_id = (item.get("image_id") or "").strip() or "（缺失）"
        lines.append(f"- {name}（image_id: {image_id}）")
    lines.append("</uploaded_files>")
    return "\n".join(lines)


def attachments_fragment(
    getter: Callable[[], list[dict[str, str]] | None],
) -> ContextFragment:
    """做成回调：每次请求的附件清单不同，每轮取当前值。"""

    def fragment() -> str | None:
        return render_attachments_context(getter())

    return fragment
