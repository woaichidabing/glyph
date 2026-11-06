"""周节奏按天替换：只改这天 / 改模板并把本周未完成的按新节奏重建。"""

from __future__ import annotations

from glyph.plan.store import PlanStore


def _store(tmp_path, today="2026-11-05"):  # 周四
    return PlanStore(root=tmp_path, today=lambda: today)


def _texts(snap, day):
    return [t["t"] for t in snap["tasks"] if t.get("scheduledFor") == day]


def test_replace_day_only_that_day(tmp_path):
    store = _store(tmp_path)
    store.set_rhythm({"4": [{"t": "背词 30 分钟"}], "5": [{"t": "写作 20 分钟"}]})
    snap = store.replace_day("2026-11-06", ["写作 40 分钟"], scope="day")
    assert _texts(snap, "2026-11-06") == ["写作 40 分钟"]
    # 模板没被改
    assert store.snapshot()["rhythm"]["5"][0]["t"] == "写作 20 分钟"


def test_replace_day_weekly_updates_template_and_week(tmp_path):
    store = _store(tmp_path)
    store.set_rhythm({"4": [{"t": "背词 30 分钟"}], "5": [{"t": "写作 20 分钟"}]})
    snap = store.replace_day("2026-11-06", ["写作 40 分钟", "听力 20 分钟"], scope="weekly")
    assert _texts(snap, "2026-11-06") == ["写作 40 分钟", "听力 20 分钟"]
    assert [x["t"] for x in store.snapshot()["rhythm"]["5"]] == ["写作 40 分钟", "听力 20 分钟"]


def test_completed_tasks_survive_rebuild(tmp_path):
    store = _store(tmp_path)
    store.set_rhythm({"4": [{"t": "背词 30 分钟"}]})
    first = store.snapshot()
    done = [t for t in first["tasks"] if t["scheduledFor"] == "2026-11-05"][0]
    store.toggle_task(done["id"], done=True)
    snap = store.replace_day("2026-11-05", ["背词 20 分钟"], scope="weekly")
    kept = [t for t in snap["tasks"] if t["scheduledFor"] == "2026-11-05" and t["done"]]
    assert kept and kept[0]["t"] == "背词 30 分钟"
    assert "背词 20 分钟" in _texts(snap, "2026-11-05")
