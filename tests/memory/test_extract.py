"""提炼器测试（D27）：把一批对话变成暂存材料。

用假模型（R6：默认测试不联网）。
"""

import json
from datetime import date, datetime, timedelta, timezone

from glyph.memory.extract import MemoryExtractor
from glyph.memory.pending import list_pending

_TZ = timezone(timedelta(hours=8))



def at(hour: int, minute: int = 0, *, day: int = 15) -> datetime:
    return datetime(2026, 10, day, hour, minute, 0, tzinfo=_TZ)



class FakeLLM:
    """剧本式假模型：按顺序吐出预设回复，并记下收到的提示词。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[object]] = []

    async def complete(self, messages, **_kwargs) -> str:
        self.calls.append(list(messages))
        if not self._replies:
            return json.dumps({"memories": []})
        return self._replies.pop(0)



def make_extractor(tmp_path, replies: list[str], **overrides) -> tuple[MemoryExtractor, FakeLLM]:
    llm = FakeLLM(replies)
    ext = MemoryExtractor(
        root=tmp_path,
        llm=llm,
        model="qwen3.7-flash",
        max_rounds=overrides.pop("max_rounds", 4),
        **overrides,
    )
    return ext, llm



TRANSCRIPT = [
    {"role": "user", "text": "我老是分不清 go 和 went"},
    {"role": "assistant", "text": "went 是 go 的过去式"},
]



def saved_reply(*titles: str) -> str:
    return json.dumps(
        {"memories": [{"type": "mistake", "title": t, "quote": "原话"} for t in titles]},
        ensure_ascii=False,
    )



# ---- 基本流程 ----


async def test_extract_writes_pending_file(tmp_path) -> None:
    ext, _ = make_extractor(tmp_path, [saved_reply("过去时误用原形")])
    result = await ext.run(
        session_id="session-1", title="作文批改", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )

    assert result.saved is True
    assert result.saved_count == 1
    files = list_pending(tmp_path)
    assert len(files) == 1
    assert "过去时误用原形" in files[0].read_text(encoding="utf-8")



async def test_empty_reply_saves_nothing(tmp_path) -> None:
    """模型说没东西可记 → 不落文件（Nothing to save 的正常路径）。"""
    ext, _ = make_extractor(tmp_path, [json.dumps({"memories": []})])
    result = await ext.run(
        session_id="s1", title="聊天", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )

    assert result.saved is False
    assert result.saved_count == 0
    assert list_pending(tmp_path) == []



async def test_nothing_to_save_prose_is_tolerated(tmp_path) -> None:
    """抄 CC：没东西可记时模型只回一句 Nothing to save. —— 也算没存下。"""
    ext, _ = make_extractor(tmp_path, ["Nothing to save."])
    result = await ext.run(
        session_id="s1", title="聊天", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )
    assert result.saved is False
    assert list_pending(tmp_path) == []



# ---- 提示词里必须带的东西 ----


async def test_prompt_includes_existing_memory_titles(tmp_path) -> None:
    """抄 CC：先给现有记忆清单，要求"能改旧的就不新建"。"""
    ext, llm = make_extractor(tmp_path, [saved_reply("x")])
    await ext.run(
        session_id="s1",
        title="t",
        transcript=TRANSCRIPT,
        day=date(2026, 10, 15),
        existing_titles=["讲解用中文", "过去时误用原形"],
    )

    prompt = str(llm.calls[0])
    assert "讲解用中文" in prompt
    assert "过去时误用原形" in prompt



async def test_prompt_includes_discipline_and_judgement(tmp_path) -> None:
    ext, llm = make_extractor(tmp_path, [saved_reply("x")])
    await ext.run(
        session_id="s1", title="t", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )

    prompt = str(llm.calls[0])
    assert "Nothing to save" in prompt  # 没东西就闭嘴
    assert "不要" in prompt  # 判据（不许编 / 不许重复记）



async def test_prompt_carries_transcript_content(tmp_path) -> None:
    ext, llm = make_extractor(tmp_path, [saved_reply("x")])
    await ext.run(
        session_id="s1", title="t", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )
    prompt = str(llm.calls[0])
    assert "go 和 went" in prompt



# ---- 往返上限（CC 的 max_turns = 4）----


async def test_bad_json_is_retried_up_to_max_rounds(tmp_path) -> None:
    """模型连回几个坏的 → 到上限就放弃，不无限重试（防烧钱）。"""
    ext, llm = make_extractor(tmp_path, ["不是json", "还是不是json", "依然不是", "仍然不是"], max_rounds=4)
    result = await ext.run(
        session_id="s1", title="t", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )

    assert result.saved is False
    assert len(llm.calls) <= 4  # 上限封顶
    assert result.attempts <= 4



async def test_recovers_after_one_bad_reply(tmp_path) -> None:
    ext, llm = make_extractor(tmp_path, ["坏", saved_reply("第二次对了")])
    result = await ext.run(
        session_id="s1", title="t", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )
    assert result.saved is True
    assert result.attempts == 2



# ---- 失败要如实上报，不许静默 ----


async def test_llm_error_reports_not_saved(tmp_path) -> None:
    class Boom:
        async def complete(self, messages, **_kwargs) -> str:
            raise RuntimeError("网络炸了")

    ext = MemoryExtractor(root=tmp_path, llm=Boom(), model="m", max_rounds=2)
    result = await ext.run(
        session_id="s1", title="t", transcript=TRANSCRIPT, day=date(2026, 10, 15)
    )

    assert result.saved is False
    assert result.error  # 有错就要有交代，别静默



# ---- 空材料不该调模型（省 token）----


async def test_empty_transcript_skips_model_call(tmp_path) -> None:
    ext, llm = make_extractor(tmp_path, [saved_reply("x")])
    result = await ext.run(
        session_id="s1", title="t", transcript=[], day=date(2026, 10, 15)
    )

    assert result.saved is False
    assert llm.calls == []  # 一个字都没有，不该花钱



async def test_no_prose_transcript_skips_model_call(tmp_path) -> None:
    """只有工具调用、没有用户文字 → 跳过（D27 的第一条过滤）。"""
    ext, llm = make_extractor(tmp_path, [saved_reply("x")])
    tools_only = [{"role": "tool", "text": "grade_text -> ok"}]
    result = await ext.run(
        session_id="s1", title="t", transcript=tools_only, day=date(2026, 10, 15)
    )

    assert result.saved is False
    assert llm.calls == []

