"""记忆装配工厂测试：共享 store，五件套 + 注入器一条线接通。"""

import pytest

from glyph.memory import MemoryCatalogInjector, MemoryStore
from glyph.tools.factory import build_memory
from glyph.tools.memory_tools import (
    ConfirmMasteryTool,
    ForgetTool,
    ReadMemoryTool,
    RecordReviewResultTool,
    RememberTool,
)


def test_build_memory_returns_store_five_tools_and_injector(tmp_path) -> None:
    store, tools, injector = build_memory(root=tmp_path)

    assert isinstance(store, MemoryStore)
    assert isinstance(injector, MemoryCatalogInjector)
    assert [t.name for t in tools] == [
        "remember",
        "record_review_result",
        "confirm_mastery",
        "forget",
        "read_memory",
    ]
    assert all(
        isinstance(t, expected)
        for t, expected in zip(
            tools,
            (
                RememberTool,
                RecordReviewResultTool,
                ConfirmMasteryTool,
                ForgetTool,
                ReadMemoryTool,
            ),
            strict=True,
        )
    )


@pytest.mark.asyncio
async def test_write_then_injector_sees_it_through_shared_store(tmp_path) -> None:
    store, tools, injector = build_memory(root=tmp_path)

    # D30：一条记忆都没有时整块不注入（原来会渲染一个「空目录」外壳，
    # 模型据此以为"里面该有东西"，真机里编出过一条不存在的到期复习）
    assert injector.fragment() is None

    remember = next(t for t in tools if t.name == "remember")
    await remember.run({"content": "讲解用中文"})

    after = injector.fragment()
    assert after is not None
    assert "讲解用中文" in after
    assert len(store.list_entries()) == 1
