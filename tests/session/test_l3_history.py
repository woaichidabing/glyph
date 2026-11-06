"""L3 压缩在 history 上的切片 / 续接重组 / C5 解压（方案 §9.5 / §9.6 / §11，v2.1 #65）。

钉四件事：
1. 有 boundary 且桌子不够大 → body 只留切点之后 tail，旧原文不再进模型；
2. 续接 user 带最后摘要（trust 成对）+ 从**全量流水**重扫的提货单（跨 boundary 不丢成品）；
3. C5 切大档（全量放得下）→ 忽略 boundary 全量渲染、记录不删；切回小档 → 切片恢复；
4. 缓存键随窗口档位变化，切档后不命中旧视图。
"""

from __future__ import annotations

import pytest

from glyph.session import new_session
from glyph.session.records import CompactBoundaryPayload, SummaryPayload
from glyph.types import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolCallBlock,
    ToolResult,
    UserMessage,
)

SUMMARY = "## 学习者画像\n他在练一般现在时，第三人称单数常漏 s。\n## 任务时间线\n改过一篇作文。"


async def _grade(history, cid: str, text: str, output: dict) -> None:
    await history.record_assistant_message(
        AssistantMessage(
            blocks=[ToolCallBlock(id=cid, name="grade_text", arguments={"text": text})]
        )
    )
    await history.record_tool_result(
        cid, ToolResult(tool_name="grade_text", ok=True, output=output)
    )


async def _compressed_history(tmp_path, *, old_text: str, with_grade: bool, cap_tokens: int):
    """造一段：旧往返（可能含一篇批改成品）→ summary + boundary → 压缩后的新 user。

    ``cap_tokens`` 直接设定渲染时快照的有效窗口 W，用来稳定触发"切片"或"解压"两条路径
    （真实档位 30 万 / 100 万在 #21 端到端用真实长会话验证）。
    """
    history = await new_session(system_prompt="sys", model="m", cwd=str(tmp_path), root=tmp_path)
    await history.record_user_message(old_text)
    if with_grade:
        await _grade(
            history,
            "g1",
            "My old essay title. He go to school every day.",
            {
                "summary": "三单漏 s",
                "detections": [
                    {"id": "d1", "original": "go", "corrected": "goes", "tier": "error"}
                ],
            },
        )
    else:
        await history.record_assistant_message(
            AssistantMessage(blocks=[TextBlock(text="好的，我看过了。")])
        )

    cut_seq = history._records[-1].seq
    await history._append(
        SummaryPayload(text=SUMMARY, covered_up_to_seq=cut_seq, model="qwen3.7-flash")
    )
    summary_seq = history._records[-1].seq
    await history._append(
        CompactBoundaryPayload(boundary_id="b1", at_seq=cut_seq, summary_record_seq=summary_seq)
    )
    await history.record_user_message("压缩后的新问题")

    history._context_window_tokens = cap_tokens
    return history


def _user_texts(messages) -> list[str]:
    return [m.content for m in messages if isinstance(m, UserMessage)]


@pytest.mark.asyncio
async def test_slices_body_and_injects_resume_user(tmp_path) -> None:
    history = await _compressed_history(
        tmp_path, old_text="x" * 20_000, with_grade=False, cap_tokens=10_000
    )

    messages = history.messages()

    assert isinstance(messages[0], SystemMessage)
    user_texts = _user_texts(messages)
    # 无 context 注入：第一条 user 就是续接摘要，其后是 tail 里的新 user。
    resume = user_texts[0]
    assert "## 学习者画像" in resume
    assert resume.count("<LEARNER_TEXT>") == 1
    assert resume.count("</LEARNER_TEXT>") == 1
    assert "read_artifact" in resume

    # 旧的大段原文被切在 boundary 之前，不出现在任何 user 消息里。
    assert all("x" * 20_000 not in text for text in user_texts)
    # 压缩后的新对话原文原样保留在 tail。
    assert any("压缩后的新问题" in text for text in user_texts)
    # 整个视图里恰好一个 trust 袋（不重复、不跨段）。
    assert sum(text.count("<LEARNER_TEXT>") for text in user_texts) == 1


@pytest.mark.asyncio
async def test_resume_picklist_lists_artifact_before_boundary(tmp_path) -> None:
    history = await _compressed_history(
        tmp_path,
        old_text="帮我批改这篇：" + "x" * 20_000,
        with_grade=True,
        cap_tokens=10_000,
    )

    resume = _user_texts(history.messages())[0]

    # 提货单从全量流水重扫（§9.6）：boundary 之前那篇成品仍在单上、可被 read_artifact 取回。
    assert "My old essay title" in resume
    assert "g1" in resume
    # 提货单是系统目录，必须在 trust 袋子外面（不圈进 learner-trust 区，C4）。
    assert resume.index("My old essay title") > resume.index("</LEARNER_TEXT>")


@pytest.mark.asyncio
async def test_c5_decompresses_when_window_is_large(tmp_path) -> None:
    history = await _compressed_history(
        tmp_path, old_text="x" * 20_000, with_grade=False, cap_tokens=10_000_000
    )

    user_texts = _user_texts(history.messages())

    # 桌子够大：忽略 boundary 全量渲染，旧原文回来；没有续接摘要 user。
    assert any("x" * 20_000 in text for text in user_texts)
    assert all("<LEARNER_TEXT>" not in text for text in user_texts)
    # boundary / summary 记录始终保留在流水里（可逆、不删）。
    assert history._last_compact_boundary() is not None


@pytest.mark.asyncio
async def test_c5_recompresses_after_window_shrinks_again(tmp_path) -> None:
    history = await _compressed_history(
        tmp_path, old_text="x" * 20_000, with_grade=False, cap_tokens=10_000_000
    )

    # 大档：全量。
    assert any("x" * 20_000 in t for t in _user_texts(history.messages()))

    # 切回小档：缓存键含窗口档位，应自动重算为切片视图（无需手动清缓存）。
    history._context_window_tokens = 10_000
    user_texts = _user_texts(history.messages())
    assert all("x" * 20_000 not in text for text in user_texts)
    assert any("<LEARNER_TEXT>" in text for text in user_texts)
    # 记录仍在，再切回大档还能解压。
    history._context_window_tokens = 10_000_000
    assert any("x" * 20_000 in t for t in _user_texts(history.messages()))
