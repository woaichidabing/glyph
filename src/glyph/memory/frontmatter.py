"""零依赖的 ``key: value`` frontmatter 解析与序列化（不引 PyYAML）。

手写文件是逃生门，所以解析必须**容错**：开头没有闭合围栏 → 当作没有 frontmatter；
不成 ``key: value`` 形状的行跳过而不是报错。值一律是字符串，类型含义由读的人按
字段表解释（count 转 int、approximate 看 true/false）。
"""

from __future__ import annotations

__all__ = ["split_frontmatter", "render_frontmatter"]

_FENCE = "---"


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出开头 frontmatter：返回 (字段字典, 剩余正文)。

    要求第一行就是 ``---`` 且在正文前出现第二个 ``---``；其它形状一律返回
    空字典、原文不动 —— 容错方向是"宁可读不到元数据也不炸"。
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FENCE:
        return {}, text

    fields: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == _FENCE:
            body = "\n".join(lines[index + 1 :])
            return fields, body.strip("\n")
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    # 只有开头围栏、没有闭合：按"无 frontmatter"处理，整段当正文
    return {}, text


def render_frontmatter(fields: dict[str, str]) -> str:
    """序列化为 ``---\\nkey: value\\n...\\n---\\n``；按 key 排序保证确定性。"""
    lines = [_FENCE]
    for key in sorted(fields):
        lines.append(f"{key}: {fields[key]}")
    lines.append(_FENCE)
    return "\n".join(lines) + "\n"
