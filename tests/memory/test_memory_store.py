"""MemoryStore 测试（真实临时目录、离线）。"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from glyph.memory import (
    MemoryEntry,
    MemoryEntryNotFoundError,
    MemoryStore,
    MemoryStoreError,
    MemoryType,
)
from glyph.memory.mastery import MasteryStage, point_from_entry
from glyph.memory.paths import entry_path, review_log_path

_TZ = timezone(timedelta(hours=8))


def make_clock(day: int):
    def _clock() -> datetime:
        return datetime(2026, 9, day, 17, 0, 0, tzinfo=_TZ)

    return _clock


def make_entry(**overrides) -> MemoryEntry:
    base = dict(
        key="",
        type=MemoryType.PREFERENCE,
        title="讲解用中文",
        quote="以后讲解都用中文",
        count=1,
        first_seen="2026-09-25",
        last_seen="2026-09-25",
        last_mentioned_at="2026-09-25",
    )
    base.update(overrides)
    return MemoryEntry(**base)


def test_upsert_creates_then_get_returns_same_entry(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    entry = make_entry(key="abcdef012345")

    action = store.upsert(entry)

    assert action == "created"
    fetched = store.get("abcdef012345")
    assert fetched is not None
    assert fetched.title == "讲解用中文"
    assert fetched.quote == "以后讲解都用中文"
    assert fetched.count == 1
    assert entry_path(tmp_path, MemoryType.PREFERENCE, "abcdef012345").exists()


def test_upsert_same_key_increments_count(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(make_entry(key="abcdef012345"))

    # 第二天又提了一次
    store.clock = make_clock(26)
    action = store.upsert(make_entry(key="abcdef012345"))

    assert action == "updated"
    fetched = store.get("abcdef012345")
    assert fetched.count == 2
    assert fetched.first_seen == "2026-09-25"
    assert fetched.last_seen == "2026-09-26"
    assert fetched.last_mentioned_at == "2026-09-26"
    # 仍然只有一个条目文件（另有给人看的 MEMORY.md 不算条目）
    entry_files = [p for p in tmp_path.glob("*.md") if p.name != "MEMORY.md"]
    assert len(entry_files) == 1


def test_list_entries_ignores_trash_and_corrupt(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(make_entry(key="abcdef012345"))
    store.upsert(make_entry(key="bcdef0123456", title="别用 emoji"))

    # 回收站与坏文件不该被扫到
    trash = tmp_path / ".trash"
    trash.mkdir()
    (trash / "fact-deadbeefdead.md").write_text("garbage", encoding="utf-8")
    (tmp_path / "preference-corrupt0000.md").write_text("完全没有frontmatter", encoding="utf-8")

    entries = store.list_entries()

    keys = sorted(e.key for e in entries)
    assert keys == ["abcdef012345", "bcdef0123456"]


def test_list_entries_skips_undecodable_file_and_counts_it(tmp_path) -> None:
    """一个不是 UTF-8 的坏文件（比如手改存成 GBK）必须被跳过，且留下计数。

    对照 Claude Code：坏历史行"跳过 + 分类计数上报"，绝不让一个坏文件堵死整个功能。
    """
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(make_entry(key="abcdef012345"))
    # 文件名合规（12 位 hex）但内容不是 UTF-8 —— 正是"手改存成 GBK"那种情形
    (tmp_path / "preference-badbad000001.md").write_bytes("标题：乱码内容".encode("gbk"))

    entries = store.list_entries()

    assert [e.key for e in entries] == ["abcdef012345"]
    # 坏文件要能被看见（单机桌面端没人盯 stderr，计数得能从对象上读到）
    assert store.skipped_files == 1


def test_get_missing_raises(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))

    with pytest.raises(MemoryEntryNotFoundError):
        store.get("000000000000")


def test_soft_delete_moves_file_into_trash(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(make_entry(key="abcdef012345"))

    trashed = store.soft_delete("abcdef012345")

    assert trashed.exists()
    assert not entry_path(tmp_path, MemoryType.PREFERENCE, "abcdef012345").exists()
    assert store.list_entries() == []
    assert "preference-abcdef012345" in trashed.name


def test_soft_delete_missing_raises(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))

    with pytest.raises(MemoryEntryNotFoundError):
        store.soft_delete("000000000000")


def test_mistake_entry_roundtrip_preserves_fields(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    entry = MemoryEntry(
        key="cafe00000001",
        type=MemoryType.MISTAKE,
        title="主谓不一致",
        quote="I goes to school",
        count=1,
        first_seen="2026-09-25",
        last_seen="2026-09-25",
        last_mentioned_at="2026-09-25",
        tier="error",
        issue_type="hard",
        original="I goes to school",
        correction="I go to school",
        explanation="主语 I 配动词原形",
    )
    store.upsert(entry)

    fetched = store.get("cafe00000001")

    assert fetched.type is MemoryType.MISTAKE
    assert fetched.tier == "error"
    assert fetched.issue_type == "hard"
    assert fetched.original == "I goes to school"
    assert fetched.correction == "I go to school"
    assert fetched.explanation == "主语 I 配动词原形"


def test_list_entries_counts_file_with_broken_frontmatter(tmp_path) -> None:
    """frontmatter 缺必填字段（手改坏了）也必须计数。

    skipped_files 的存在理由就是"我明明记过，它怎么不见了"要能自查；
    只统计"解码失败"、漏掉"字段坏了"，这个抓手就等于半废 ——
    手改坏 frontmatter 比存成 GBK 常见得多。
    """
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(make_entry(key="abcdef012345"))
    # 文件名合规（key 必须是 12 位十六进制，badbad000002 符合）、内容也是合法 UTF-8，
    # 但 frontmatter 缺 type / count 等必填项
    (tmp_path / "preference-badbad000002.md").write_text(
        "---\ntitle: 手改坏了\n---\n\n## 用户原话\n某句话\n", encoding="utf-8"
    )

    entries = store.list_entries()

    assert [e.key for e in entries] == ["abcdef012345"]
    assert store.skipped_files == 1


# ---------- 掌握度账本字段（v2，第二棒第②片）----------


def _mastery_mistake(**overrides) -> MemoryEntry:
    base = dict(
        key="cafe000000aa",
        type=MemoryType.MISTAKE,
        title="一般过去时",
        quote="yesterday I go home",
        count=1,
        first_seen="2026-09-25",
        last_seen="2026-09-25",
        last_mentioned_at="2026-09-25",
        tier="error",
        issue_type="hard",
        error_code="VERB:TENSE",
        original="I go home yesterday",
        correction="I went home yesterday",
        explanation="过去时间状语配过去式",
        mastery_stage="learning",
        independent_streak=1,
        due_date="2026-09-28",
        good_count=1,
        hard_count=0,
        wrong_count=0,
        last_review_at="2026-09-25",
        reopened_count=0,
    )
    base.update(overrides)
    return MemoryEntry(**base)


# ---------- ⑤-5c：一个知识点一本账（借壳代表条目，D15/D20）----------


def _plain_mistake(**overrides) -> MemoryEntry:
    """无账本的普通错题（可带 error_code，作为同知识点的证据 / 出题种子）。"""
    base = dict(
        key="",
        type=MemoryType.MISTAKE,
        title="错",
        quote="q",
        count=1,
        first_seen="2026-09-25",
        last_seen="2026-09-25",
        last_mentioned_at="2026-09-25",
        tier="error",
        issue_type="hard",
        original="o",
        correction="c",
    )
    base.update(overrides)
    return MemoryEntry(**base)


def test_evidence_mistake_persists_code_without_ledger(tmp_path) -> None:
    # D15：同知识点后续错句是"证据"，带 code 但不背账本（无 stage）
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_plain_mistake(key="eeee00000001", error_code="VERB:TENSE"))

    got = store.get("eeee00000001")
    assert got.error_code == "VERB:TENSE"
    assert got.mastery_stage is None  # 不背账本
    text = (tmp_path / "mistake-eeee00000001.md").read_text(encoding="utf-8")
    assert "error_code: VERB:TENSE" in text
    assert "mastery_stage" not in text  # 账本字段仍不写


def test_find_mastery_point_by_code_returns_representative_only(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake(key="aaaa00000001"))  # 已建档代表 learning
    store.upsert(_plain_mistake(key="aaaa00000002", error_code="VERB:TENSE"))  # 同码证据
    store.upsert(_mastery_mistake(key="bbbb00000003", error_code="DET"))  # 另一知识点代表

    point = store.find_mastery_point_by_code("VERB:TENSE")
    assert point is not None
    assert point.key == "aaaa00000001"  # 返回已建档代表，不是证据句
    assert point.stage is MasteryStage.LEARNING
    assert store.find_mastery_point_by_code("SPELL") is None


def test_upsert_cannot_change_enrolled_representative_code(tmp_path) -> None:
    # D20：error_code 一经建档就是身份，后续写入（哪怕也带着账本阶段）都不能改它
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake(key="aaaa00000001", error_code="VERB:TENSE"))

    store.upsert(_mastery_mistake(key="aaaa00000001", error_code="DET"))  # 试图改码

    assert store.get("aaaa00000001").error_code == "VERB:TENSE"


def test_soft_delete_representative_transfers_ledger_to_evidence(tmp_path) -> None:
    # D20：删掉背账代表时，账本进度过户给同知识点最早的证据句，进度不丢
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(
        _mastery_mistake(
            key="aaaa00000001",
            error_code="VERB:TENSE",
            mastery_stage="learning",
            independent_streak=2,
            due_date="2026-09-28",
            good_count=2,
        )
    )
    store.clock = make_clock(26)
    store.upsert(_plain_mistake(key="aaaa00000002", error_code="VERB:TENSE"))  # 同码证据

    store.soft_delete("aaaa00000001")

    with pytest.raises(MemoryEntryNotFoundError):
        store.get("aaaa00000001")  # 代表已进回收站
    heir = store.find_mastery_point_by_code("VERB:TENSE")
    assert heir is not None
    assert heir.key == "aaaa00000002"  # 证据成为新代表
    assert heir.stage is MasteryStage.LEARNING
    assert heir.streak == 2  # 账本进度完整过户
    assert heir.due_date == date(2026, 9, 28)
    assert heir.good_count == 2


def test_soft_delete_transfer_write_failure_keeps_representative_intact(
    tmp_path, monkeypatch
) -> None:
    # 过户安全顺序：把账本写进新代表这步若失败（磁盘满 / 文件被占用），
    # 旧代表必须还原封不动——账本不能因为一次失败的删除而静默丢失。
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(
        _mastery_mistake(
            key="aaaa00000001",
            error_code="VERB:TENSE",
            mastery_stage="learning",
            independent_streak=2,
            due_date="2026-09-28",
            good_count=2,
        )
    )
    store.clock = make_clock(26)
    store.upsert(_plain_mistake(key="aaaa00000002", error_code="VERB:TENSE"))

    heir_path = entry_path(tmp_path, MemoryType.MISTAKE, "aaaa00000002")
    real_atomic_write = store._atomic_write

    def flaky_write(path, text) -> None:
        if path == heir_path:  # 只在过户写证据句这一步炸
            raise MemoryStoreError("写不了记忆文件 aaaa00000002: disk full")
        real_atomic_write(path, text)

    monkeypatch.setattr(store, "_atomic_write", flaky_write)

    with pytest.raises(MemoryStoreError):
        store.soft_delete("aaaa00000001")

    # 旧代表没被移进回收站，账本进度完好
    point = point_from_entry(store.get("aaaa00000001"))
    assert point is not None
    assert point.key == "aaaa00000001"
    assert point.stage is MasteryStage.LEARNING
    assert point.streak == 2
    assert point.good_count == 2
    # 证据句没被提前改成背账（不留半截过户状态）
    assert point_from_entry(store.get("aaaa00000002")) is None


def test_soft_delete_lone_representative_removes_ledger_but_keeps_others(tmp_path) -> None:
    # 没有同码证据可过户：连账一起消失；别的知识点账本不受影响
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake(key="aaaa00000001", error_code="VERB:TENSE"))
    store.upsert(_mastery_mistake(key="bbbb00000003", error_code="DET"))

    store.soft_delete("aaaa00000001")

    assert store.find_mastery_point_by_code("VERB:TENSE") is None
    det = store.find_mastery_point_by_code("DET")
    assert det is not None and det.key == "bbbb00000003"


def test_soft_delete_evidence_keeps_representative_ledger(tmp_path) -> None:
    # 删的是证据句（不背账），代表账本纹丝不动
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake(key="aaaa00000001", error_code="VERB:TENSE"))
    store.upsert(_plain_mistake(key="aaaa00000002", error_code="VERB:TENSE"))

    store.soft_delete("aaaa00000002")

    with pytest.raises(MemoryEntryNotFoundError):
        store.get("aaaa00000002")
    point = store.find_mastery_point_by_code("VERB:TENSE")
    assert point is not None and point.key == "aaaa00000001"


def test_mistake_with_mastery_roundtrips_and_renders_section(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake())

    got = store.get("cafe000000aa")
    assert got.mastery_stage == "learning"
    assert got.error_code == "VERB:TENSE"
    assert got.independent_streak == 1
    assert got.due_date == "2026-09-28"
    assert (got.good_count, got.hard_count, got.wrong_count) == (1, 0, 0)
    assert got.last_review_at == "2026-09-25"
    assert got.reopened_count == 0
    # 批改粗桶与 ERRANT 细标签并存、互不覆盖
    assert got.issue_type == "hard"
    assert got.tier == "error"

    text = (tmp_path / "mistake-cafe000000aa.md").read_text(encoding="utf-8")
    assert "## 掌握度" in text  # 给人看的小节
    assert "VERB:TENSE" in text
    assert "mastery_stage: learning" in text  # frontmatter 真相


def test_legacy_v1_mistake_reads_without_mastery(tmp_path) -> None:
    """v1 旧错题没有掌握度字段：照常读取、字段为 None、不跳过、不自动进复测。"""
    legacy = (
        "---\n"
        "count: 2\n"
        "first_seen: 2026-09-01\n"
        "issue_type: hard\n"
        "key: cafe000000bb\n"
        "last_mentioned_at: 2026-09-10\n"
        "last_seen: 2026-09-10\n"
        "tier: error\n"
        "title: 旧错题\n"
        "type: mistake\n"
        "version: 1\n"
        "---\n\n"
        "# 旧错题\n\n"
        "## 用户原话\nI goes\n\n"
        "## 原文片段\nI goes\n\n"
        "## 建议改法\nI go\n"
    )
    (tmp_path / "mistake-cafe000000bb.md").write_text(legacy, encoding="utf-8")
    store = MemoryStore(root=tmp_path, clock=make_clock(25))

    got = store.get("cafe000000bb")
    assert got.version == 1
    assert got.issue_type == "hard"
    assert got.mastery_stage is None
    assert got.error_code is None
    assert got.independent_streak is None
    assert got.due_date is None
    assert store.skipped_files == 0


def test_new_mistake_without_mastery_does_not_write_mastery_fields(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(
        MemoryEntry(
            key="cafe000000cc",
            type=MemoryType.MISTAKE,
            title="普通错",
            quote="q",
            count=1,
            first_seen="2026-09-25",
            last_seen="2026-09-25",
            last_mentioned_at="2026-09-25",
            tier="error",
            issue_type="chinglish",
            original="o",
            correction="c",
        )
    )

    text = (tmp_path / "mistake-cafe000000cc.md").read_text(encoding="utf-8")
    assert "mastery_stage" not in text
    assert "error_code" not in text
    assert store.get("cafe000000cc").mastery_stage is None


def test_enrolling_mastery_upgrades_legacy_v1_to_v2(tmp_path) -> None:
    legacy = (
        "---\n"
        "count: 2\n"
        "first_seen: 2026-09-01\n"
        "issue_type: hard\n"
        "key: cafe000000dd\n"
        "last_mentioned_at: 2026-09-10\n"
        "last_seen: 2026-09-10\n"
        "tier: error\n"
        "title: 旧错题\n"
        "type: mistake\n"
        "version: 1\n"
        "---\n\n"
        "# 旧错题\n\n## 用户原话\napple\n\n"
        "## 原文片段\nI have two apple\n\n## 建议改法\nI have two apples\n"
    )
    (tmp_path / "mistake-cafe000000dd.md").write_text(legacy, encoding="utf-8")
    store = MemoryStore(root=tmp_path, clock=make_clock(26))

    # 模拟第⑤片"采纳硬错 → 建档"：在旧错题上补 ERRANT 代码与掌握度状态后落盘
    entry = store.get("cafe000000dd")
    entry.error_code = "NOUN:NUM"
    entry.mastery_stage = "new"
    entry.independent_streak = 0
    entry.due_date = "2026-09-27"
    entry.good_count = entry.hard_count = entry.wrong_count = 0
    entry.reopened_count = 0
    assert store.upsert(entry) == "updated"

    got = store.get("cafe000000dd")
    assert got.version == 2
    assert got.mastery_stage == "new"
    assert got.error_code == "NOUN:NUM"
    assert got.due_date == "2026-09-27"
    assert got.issue_type == "hard"  # 粗桶不丢


def test_corrupt_mastery_int_is_tolerated_not_skipped(tmp_path) -> None:
    """掌握度的某个整数被手改坏：增强字段容错为 None，但整条记忆不能被跳过。"""
    broken = (
        "---\n"
        "count: 1\n"
        "due_date: 2026-09-28\n"
        "error_code: VERB:TENSE\n"
        "first_seen: 2026-09-25\n"
        "independent_streak: abc\n"
        "issue_type: hard\n"
        "key: cafe000000ee\n"
        "last_mentioned_at: 2026-09-25\n"
        "last_seen: 2026-09-25\n"
        "mastery_stage: learning\n"
        "tier: error\n"
        "title: 坏计数\n"
        "type: mistake\n"
        "version: 2\n"
        "---\n\n# 坏计数\n\n## 用户原话\nq\n"
    )
    (tmp_path / "mistake-cafe000000ee.md").write_text(broken, encoding="utf-8")
    store = MemoryStore(root=tmp_path, clock=make_clock(25))

    entries = store.list_entries()
    assert [e.key for e in entries] == ["cafe000000ee"]
    assert store.skipped_files == 0
    assert entries[0].independent_streak is None
    assert entries[0].mastery_stage == "learning"  # 其余掌握度字段仍读出


def test_plain_upsert_keeps_existing_mastery_ledger(tmp_path) -> None:
    """已建档错题被通用"记住"再次命中（新 entry 不带账本字段）时，进度不得被抹掉。

    掌握度是硬状态：销账/清档只能走专门的掌握度操作；一次普通 Remember 命中
    同一条错题，只累加次数与日期，账本（阶段/连对/到期/复活）必须原样保留。
    """
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake())  # 已建档 learning、连对 1、due 09-28

    plain = MemoryEntry(
        key="cafe000000aa",
        type=MemoryType.MISTAKE,
        title="一般过去时",
        quote="yesterday I go home",
        count=1,
        first_seen="2026-09-26",
        last_seen="2026-09-26",
        last_mentioned_at="2026-09-26",
        tier="error",
        issue_type="hard",
        original="I go home yesterday",
        correction="I went home yesterday",
    )
    store.clock = make_clock(26)
    assert store.upsert(plain) == "updated"

    got = store.get("cafe000000aa")
    assert got.count == 2  # 通用记住照常累加
    assert got.last_seen == "2026-09-26"
    assert got.mastery_stage == "learning"  # 账本没被抹掉
    assert got.error_code == "VERB:TENSE"
    assert got.independent_streak == 1
    assert got.due_date == "2026-09-28"
    assert (got.good_count, got.hard_count, got.wrong_count) == (1, 0, 0)
    assert got.reopened_count == 0


# ---------- 复测流水 reviews（第二棒第④片）：sidecar JSONL，append-only ----------


def test_append_and_list_reviews_roundtrip_with_free_text(tmp_path) -> None:
    """题干 / 答案 / 讲解是自由文本（含换行、引号、中文、markdown），JSON 转义必须安全往返；
    流水按追加顺序读回，且落在 reviews/ 子目录、不污染顶层记忆扫描、不重写 MEMORY.md。"""
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    key = "cafe000000aa"
    rec1 = {
        "date": "2026-10-02",
        "verdict": "independent",
        "question": "用 yesterday 改写：I ___ (go) home yesterday.",
        "answer": "I went home yesterday.",
        "explanation": '过去时间状语，动词用过去式\n第二行带**换行**、引号 " 和中文时态。',
    }
    rec2 = {
        "date": "2026-10-05",
        "verdict": "wrong",
        "question": "q2",
        "answer": "a2",
        "explanation": "",
    }

    path = store.append_review(key, rec1)
    store.append_review(key, rec2)

    assert path == review_log_path(tmp_path, key)
    assert path.exists()
    assert store.list_reviews(key) == [rec1, rec2]  # 顺序=追加顺序，自由文本原样往返
    # 流水在 reviews/ 子目录：顶层既扫不到条目，也不该顺手生成 MEMORY.md
    assert store.list_entries() == []
    assert list(tmp_path.glob("*.md")) == []


def test_list_reviews_missing_file_returns_empty(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))

    assert store.list_reviews("000000000000") == []
    assert store.skipped_reviews == 0


def test_list_reviews_skips_bad_lines_and_counts(tmp_path) -> None:
    """一条流水被手改坏（非法 JSON / 非对象）：跳过坏行、好行照常，并留下计数。"""
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    log = review_log_path(tmp_path, "cafe000000aa")
    log.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(
        {
            "date": "2026-10-02",
            "verdict": "hinted",
            "question": "q",
            "answer": "a",
            "explanation": "",
        },
        ensure_ascii=False,
    )
    log.write_text(good + "\n\n{not valid json\n", encoding="utf-8")

    reviews = store.list_reviews("cafe000000aa")

    assert [r["verdict"] for r in reviews] == ["hinted"]
    assert store.skipped_reviews == 1


# ---------- 掌握度专用落盘 save_mastery（第二棒第④片）----------


def test_save_mastery_updates_ledger_without_touching_count_or_dates(tmp_path) -> None:
    """记账只推进账本：count 与三个日期保持原样（复测不是"又犯一次/又提一次"）。"""
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(_mastery_mistake())  # count=1、日期 09-25、learning 连对1 due09-28
    entry = store.get("cafe000000aa")

    # 模拟第 3 次独立做对即掌握：只改账本字段
    entry.mastery_stage = "mastered"
    entry.independent_streak = 3
    entry.due_date = None
    entry.good_count = 3
    entry.last_review_at = "2026-10-02"
    store.clock = make_clock(26)  # 即使时钟往前走，落盘也不得刷日期
    store.save_mastery(entry)

    got = store.get("cafe000000aa")
    assert got.mastery_stage == "mastered"
    assert got.independent_streak == 3
    assert got.due_date is None
    assert got.good_count == 3
    assert got.last_review_at == "2026-10-02"
    # count / 身份 / 三个日期 / 正文原样
    assert got.count == 1
    assert got.first_seen == "2026-09-25"
    assert got.last_seen == "2026-09-25"
    assert got.last_mentioned_at == "2026-09-25"
    assert got.version == 2
    assert got.original == "I go home yesterday"
    assert got.correction == "I went home yesterday"
    assert got.issue_type == "hard"


def test_save_mastery_missing_key_raises_not_found(tmp_path) -> None:
    """记账不能凭空建档：磁盘上没有的 key，save_mastery 直接报错（建档是定稿那一片的事）。"""
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    ghost = _mastery_mistake(key="deadbeef0000")  # 从没落过盘

    with pytest.raises(MemoryEntryNotFoundError):
        store.save_mastery(ghost)


def test_save_mastery_rejects_unenrolled_mistake(tmp_path) -> None:
    """未建档（没有 mastery_stage / error_code）的普通错题不能走账本通道，防止被误清账。"""
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(
        MemoryEntry(
            key="cafe000000cc",
            type=MemoryType.MISTAKE,
            title="普通错",
            quote="q",
            count=1,
            first_seen="2026-09-25",
            last_seen="2026-09-25",
            last_mentioned_at="2026-09-25",
            tier="error",
            issue_type="chinglish",
            original="o",
            correction="c",
        )
    )
    plain = store.get("cafe000000cc")  # mastery_stage / error_code 都是 None

    with pytest.raises(MemoryStoreError):
        store.save_mastery(plain)


def test_save_mastery_rejects_non_mistake(tmp_path) -> None:
    store = MemoryStore(root=tmp_path, clock=make_clock(25))
    store.upsert(make_entry(key="abcdef012345"))
    pref = store.get("abcdef012345")
    pref.mastery_stage = "learning"  # 硬塞也不行：类型就不是 mistake

    with pytest.raises(MemoryStoreError):
        store.save_mastery(pref)
