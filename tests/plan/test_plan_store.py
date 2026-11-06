"""计划仓库：目标、任务、勾选、自动完成、迁移、坏文件容错。"""

from __future__ import annotations

from pathlib import Path

from glyph.plan import PlanStore
from glyph.plan.paths import state_path


def _store(tmp_path: Path, now: int = 1_760_000_000_000, today: str = "2026-10-21") -> PlanStore:
    return PlanStore(tmp_path, clock=lambda: now, today=lambda: today)


def test_env_root_is_used(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GLYPH_PLAN_ROOT", str(tmp_path))
    assert PlanStore().path == state_path(tmp_path)


def test_empty_snapshot(tmp_path: Path) -> None:
    assert _store(tmp_path).snapshot() == {
        "version": 2,
        "goal": None,
        "phases": [],
        "rhythm": {},
        "tasks": [],
    }


def test_goal_persists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snap = store.set_goal("三个月过六级", weeks=12, start_date="2026-10-21")
    assert snap["goal"]["text"] == "三个月过六级"
    assert snap["goal"]["weeks"] == 12
    assert snap["goal"]["startDate"] == "2026-10-21"
    again = PlanStore(tmp_path).snapshot()
    assert again["goal"]["text"] == "三个月过六级"


def test_tasks_infer_kind_and_done_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snap = store.add_tasks(
        [
            {"t": "背 20 个六级词"},
            {"t": "查 3 个生词", "kind": "dict", "target": 3},
            {"t": "自己加一件"},
        ]
    )
    assert [t["kind"] for t in snap["tasks"]] == ["study", "dict", "custom"]
    assert snap["tasks"][1]["target"] == 3
    tid = snap["tasks"][0]["id"]
    done = store.toggle_task(tid, done=True)
    assert done["task"]["done"] is True
    assert done["task"]["doneAt"] == 1_760_000_000_000
    undone = store.toggle_task(tid, done=False)
    assert undone["task"]["done"] is False
    assert undone["task"]["doneAt"] is None


def test_delete_task(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tid = store.add_tasks([{"t": "自己加一件"}])["tasks"][0]["id"]
    snap = store.delete_task(tid)
    assert snap["removed"]["id"] == tid
    assert snap["tasks"] == []


def test_complete_kind_increments_progress_and_completes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snap = store.add_tasks([{"t": "查 2 个生词", "kind": "dict", "target": 2}])
    tid = snap["tasks"][0]["id"]
    first = store.complete_kind("dict", evidence="查过 commit")
    assert first["task"]["progress"] == 1
    assert first["task"]["done"] is False
    second = store.complete_kind("dict", evidence="查过 reluctant")
    assert second["task"]["id"] == tid
    assert second["task"]["progress"] == 2
    assert second["task"]["done"] is True
    assert second["task"]["evidence"] == "查过 reluctant"


def test_complete_kind_ignores_future_tasks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add_tasks([{"t": "背 20 个词", "kind": "study", "scheduledFor": "2026-10-22"}])
    result = store.complete_kind("study", evidence="今天背完")
    assert result["task"] is None
    assert store.snapshot()["tasks"][0]["done"] is False


def test_import_merges_without_duplicate_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_goal("旧目标")
    snap = store.import_state(
        goal=None,
        tasks=[
            {"id": "a", "t": "自己加一件", "src": "own", "done": False},
            {"id": "a", "t": "重复", "src": "own", "done": False},
        ],
    )
    assert snap["importedTasks"] == 1
    assert len(snap["tasks"]) == 1
    assert snap["goal"]["text"] == "旧目标"


def test_bad_file_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    assert _store(tmp_path).snapshot()["tasks"] == []


def test_rhythm_materializes_today(tmp_path: Path) -> None:
    """周节奏：今天该练的自动变成今天的任务，且同一天只生成一次。"""
    store = _store(tmp_path, today="2026-11-04")  # 周三
    store.set_rhythm({"3": [{"t": "背词 30 分钟", "kind": "study"}, {"t": "写 1 篇作文"}]})
    first = store.snapshot()
    today_tasks = [x for x in first["tasks"] if x["scheduledFor"] == "2026-11-04"]
    assert [x["t"] for x in today_tasks] == ["背词 30 分钟", "写 1 篇作文"]
    assert all(x["done"] is False for x in today_tasks)
    # 幂等：再读一次不会翻倍
    second = store.snapshot()
    assert len(second["tasks"]) == len(first["tasks"])


def test_rhythm_other_weekday_left_alone(tmp_path: Path) -> None:
    """周二写的节奏，周三打开时不该凭空出现。"""
    store = _store(tmp_path, today="2026-11-04")
    store.set_rhythm({"2": [{"t": "周二听力"}]})
    assert store.snapshot()["tasks"] == []


def test_phases_roundtrip_and_cleaning(tmp_path: Path) -> None:
    store = _store(tmp_path, today="2026-11-04")
    snap = store.set_phases([
        {"fromWeek": 1, "toWeek": 4, "title": "打底", "focus": "高频词 + 听力"},
        {"fromWeek": 9, "toWeek": 4, "title": "顺序反了"},  # 应被丢掉
    ])
    assert [p["title"] for p in snap["phases"]] == ["打底"]
    assert snap["phases"][0]["focus"] == "高频词 + 听力"


def test_reset_clears_everything(tmp_path: Path) -> None:
    """换计划：reset 之后目标、阶段、节奏、任务全空。"""
    store = _store(tmp_path, today="2026-11-04")
    store.set_goal("三个月过四级")
    store.set_phases([{"fromWeek": 1, "toWeek": 4, "title": "打底"}])
    store.set_rhythm({"3": [{"t": "背词 30 分钟"}]})
    store.add_tasks(["自己加一件"])
    assert store.snapshot()["tasks"]
    snap = store.reset()
    assert snap["goal"] is None
    assert snap["phases"] == []
    assert snap["rhythm"] == {}
    assert snap["tasks"] == []
