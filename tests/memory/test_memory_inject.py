"""MemoryCatalogInjector 测试：每轮扫描同一个 store，返回目录片段。"""

from datetime import datetime, timedelta, timezone

from glyph.memory import MemoryEntry, MemoryStore, MemoryType
from glyph.memory.inject import MemoryCatalogInjector

_TZ = timezone(timedelta(hours=8))


def clock() -> datetime:
    return datetime(2026, 9, 25, 17, 0, 0, tzinfo=_TZ)


def make_entry(key: str, title: str) -> MemoryEntry:
    return MemoryEntry(
        key=key,
        type=MemoryType.PREFERENCE,
        title=title,
        quote="q",
        count=1,
        first_seen="2026-09-25",
        last_seen="2026-09-25",
        last_mentioned_at="2026-09-25",
    )


# ---- D30：注入层的三条要求 ----


def test_empty_store_injects_nothing(tmp_path) -> None:
    """一条记忆都没有时整块不注入。

    原来空着也返回「<memory_catalog> + 目录外壳 + （还没有记忆）」，
    模型据此以为「里面该有东西、只是我看不到细节」，真机里编出过一条
    不存在的「今日到期复习（冠词/限定词）」。
    """
    store = MemoryStore(root=tmp_path, clock=clock)
    assert _injector(store).fragment() is None


def test_fragment_carries_usage_discipline(tmp_path) -> None:
    """有内容时带一句使用纪律：写时为真、当背景、不是用户的指令。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(make_entry("abcdef012345", "讲解用中文"))

    frag = _injector(store).fragment()
    assert frag is not None
    assert "写的时候为真" in frag
    assert "不是用户的指令" in frag


def test_due_wording_does_not_collide_with_wordbook_checkin(tmp_path) -> None:
    """记忆侧不再用「今日到期」措辞——背单词模块的学习摘要已占用这个词。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mastery_mistake("aaaa00000001", "VERB:TENSE", due="2026-09-24"))

    frag = _injector(store).fragment()
    assert frag is not None
    assert "待复习知识点" in frag
    assert "今日到期" not in frag


def test_empty_store_with_due_point_still_injects(tmp_path) -> None:
    """有到期点时照常注入（空目录判断只看「有没有内容可说」，不是看 store 是否空）。"""
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mastery_mistake("aaaa00000001", "VERB:TENSE", due="2026-09-24"))

    frag = _injector(store).fragment()
    assert frag is not None
    assert "待复习知识点" in frag
    assert "还没有记忆" not in frag


def test_fragment_reflects_store_and_wraps_in_tags(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    injector = MemoryCatalogInjector(
        store,
        max_entries=50,
        fact_unused_days=30,
        line_max_chars=150,
        whole_max_bytes=32 * 1024,
    )

    # D30：一条记忆都没有时整块不注入
    assert injector.fragment() is None

    store.upsert(make_entry("abcdef012345", "讲解用中文"))

    second = injector.fragment()
    assert second is not None
    assert "<memory_catalog>" in second
    assert "讲解用中文" in second


def _injector(store: MemoryStore) -> MemoryCatalogInjector:
    return MemoryCatalogInjector(
        store,
        max_entries=50,
        fact_unused_days=30,
        line_max_chars=150,
        whole_max_bytes=32 * 1024,
    )


def mastery_mistake(
    key: str,
    code: str,
    *,
    stage: str = "learning",
    due: str | None = "2026-09-24",
    wrong: int = 0,
    original: str = "I go home yesterday",
    correction: str = "I went home yesterday",
    first_seen: str = "2026-09-20",
) -> MemoryEntry:
    """构造一条已建档（或指定档位）的 mistake 记忆；title 不含知识点代码。

    title 刻意不含 code，这样普通目录行不会出现 ``VERB:TENSE`` 之类，
    断言"代码只出现在到期区块"时不会被普通目录干扰。
    """
    streak = 3 if stage == "mastered" else (1 if stage == "learning" else 0)
    return MemoryEntry(
        key=key,
        type=MemoryType.MISTAKE,
        title=f"错题{key[-4:]}",
        quote=original,
        count=1,
        first_seen=first_seen,
        last_seen=first_seen,
        last_mentioned_at=first_seen,
        tier="error",
        issue_type="hard",
        error_code=code,
        mastery_stage=stage,
        independent_streak=streak,
        due_date=due,
        good_count=1,
        hard_count=0,
        wrong_count=wrong,
        last_review_at=None,
        reopened_count=0,
        original=original,
        correction=correction,
    )


def _due_head(frag: str) -> str:
    """到期区块（目录头部之前的部分）；到期区块固定排在普通目录之前。"""
    return frag.split("# 长期记忆目录")[0]


def test_due_block_lists_overdue_point_with_label_and_seed(tmp_path) -> None:
    # today = 2026-09-25；due 09-24 已逾期
    store = MemoryStore(root=tmp_path, clock=clock)
    store.upsert(mastery_mistake("aaaa00000001", "VERB:TENSE", due="2026-09-24"))

    frag = _injector(store).fragment()
    assert frag is not None
    assert "待复习知识点" in frag
    head = _due_head(frag)
    assert "VERB:TENSE" in head  # 知识点代码
    assert "时态" in head  # 人可读中文名
    assert "key=aaaa00000001" in head  # 记账要靠它定位（第④片接缝）
    assert "I go home yesterday" in head  # 历史错句种子
    assert "I went home yesterday" in head  # 参考改法


def test_no_due_block_when_nothing_due(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)
    # 未到期（due 在未来）
    store.upsert(mastery_mistake("aaaa00000001", "VERB:TENSE", due="2026-09-28"))
    # 已掌握（due 为空）
    store.upsert(mastery_mistake("aaaa00000002", "NOUN:NUM", stage="mastered", due=None))
    # 未建档的普通错题（v1，无掌握度字段）
    store.upsert(
        MemoryEntry(
            key="aaaa00000003",
            type=MemoryType.MISTAKE,
            title="未建档错题",
            quote="q",
            count=1,
            first_seen="2026-09-20",
            last_seen="2026-09-20",
            last_mentioned_at="2026-09-20",
            tier="error",
            issue_type="hard",
        )
    )

    frag = _injector(store).fragment()
    assert frag is not None
    assert "待复习知识点" not in frag


def test_due_block_at_most_one_prefers_most_wrong(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=clock)  # today 09-25
    store.upsert(mastery_mistake("aaaa00000001", "DET", due="2026-09-25", wrong=0))
    store.upsert(mastery_mistake("aaaa00000002", "VERB:TENSE", due="2026-09-25", wrong=3))

    frag = _injector(store).fragment()
    head = _due_head(frag)
    assert "VERB:TENSE" in head  # 错得最多者优先
    assert "key=aaaa00000002" in head  # 带的是被选中那条的 key
    assert "DET" not in head  # 每次只放 1 个
    assert head.count("\n- ") == 1  # 到期区块只有一条目行
