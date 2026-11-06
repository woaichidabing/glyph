"""MEMORY.md（给人看的目录）测试：在跟的与已掌握分开列（D31）。

这个文件工具从不读（逃生门），但它决定用户打开文件能看到什么：
两处口径要一致——都按同一套掌握度状态分组。
"""

from datetime import datetime, timedelta, timezone

from glyph.memory.store import MemoryStore
from glyph.memory.types import MemoryEntry, MemoryType

_TZ = timezone(timedelta(hours=8))



def clock() -> datetime:
    return datetime(2026, 10, 16, 20, 0, 0, tzinfo=_TZ)



def mistake(key: str, title: str, *, stage: str | None = None, by: str | None = None):
    return MemoryEntry(
        key=key,
        type=MemoryType.MISTAKE,
        title=title,
        quote="q",
        count=1,
        first_seen="2026-10-10",
        last_seen="2026-10-16",
        last_mentioned_at="2026-10-16",
        tier="error",
        issue_type="hard",
        error_code="VERB:TENSE",
        mastery_stage=stage,
        mastered_by=by,
    )



def read_catalog(root) -> str:
    return (root / "MEMORY.md").read_text(encoding="utf-8")



def test_active_entries_listed(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mistake("aaaaaaaaaaaa", "在跟的错题", stage="learning"))

    text = read_catalog(tmp_path)
    assert "在跟的错题" in text
    assert "已掌握" not in text  # 没有已掌握的，就不出这一节



def test_mastered_entries_go_to_their_own_section(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mistake("aaaaaaaaaaaa", "在跟的错题", stage="learning"))
    store.upsert(
        mistake("bbbbbbbbbbbb", "已掌握的错题", stage="mastered", by="user_claim")
    )

    text = read_catalog(tmp_path)
    assert "已掌握" in text
    assert "已掌握的错题" in text
    # 已掌握的必须排在"已掌握"那节之后，且在跟的那节之前
    assert text.index("在跟的错题") < text.index("已掌握")



def test_mastered_entry_shows_date_and_basis(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mistake("bbbbbbbbbbbb", "已掌握的错题", stage="mastered", by="user_claim"))

    text = read_catalog(tmp_path)
    assert "2026-10-16" in text  # 掌握日期（用最近提及日）
    assert "自述" in text  # 掌握依据：自述验证



def test_streak_mastery_shows_its_own_basis(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mistake("bbbbbbbbbbbb", "连对掌握的", stage="mastered", by="streak"))

    text = read_catalog(tmp_path)
    assert "连对" in text



def test_header_explains_what_mastered_means(tmp_path) -> None:
    """别让用户以为"已掌握=删了"——要说清它还在、还会复活。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mistake("bbbbbbbbbbbb", "已掌握的", stage="mastered", by="streak"))

    text = read_catalog(tmp_path)
    assert "复活" in text



def test_non_mistake_types_unaffected(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(
        MemoryEntry(
            key="cccccccccccc",
            type=MemoryType.PREFERENCE,
            title="讲解用中文",
            quote="说中文",
            count=1,
            first_seen="2026-10-10",
            last_seen="2026-10-16",
            last_mentioned_at="2026-10-16",
        )
    )

    text = read_catalog(tmp_path)
    assert "讲解用中文" in text
    assert "已掌握" not in text



def test_removing_last_mastered_drops_the_section(tmp_path) -> None:
    """已掌握的条目被删掉后，那一节也要消失，不留空标题。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mistake("bbbbbbbbbbbb", "已掌握的", stage="mastered", by="streak"))
    assert "已掌握" in read_catalog(tmp_path)

    store.soft_delete("bbbbbbbbbbbb")
    assert "已掌握" not in read_catalog(tmp_path)

