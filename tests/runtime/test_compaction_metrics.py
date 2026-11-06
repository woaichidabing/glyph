"""[v2.1 · D2] 压缩遥测 jsonl 落盘（runtime/compaction_metrics.py）。

钉：DTO 字段齐全（设计稿 §12.3 七要素 + 时间戳）、追加成行分隔 JSON、
观察者工厂可用、**写盘失败只吞不抛**（遥测是旁路，绝不炸主循环）。
"""

from __future__ import annotations

import datetime as dt
import json

from glyph.runtime.compaction_metrics import (
    CompactionMetric,
    append_compaction_metric,
    file_compaction_observer,
)


def _metric(**over) -> CompactionMetric:
    base = dict(
        success=True,
        level="l3",
        before_tokens=249_000,
        after_tokens=120_000,
        elapsed_ms=1234.5,
        degraded_reason="",
        material_truncated=False,
        boundary_id="c-1",
    )
    base.update(over)
    return CompactionMetric(**base)


def test_to_dict_has_all_d2_fields_and_iso_timestamp() -> None:
    data = _metric().to_dict()
    for key in (
        "ts",
        "success",
        "level",
        "boundary_id",
        "before_tokens",
        "after_tokens",
        "elapsed_ms",
        "material_truncated",
        "degraded_reason",
    ):
        assert key in data
    assert data["success"] is True
    assert data["elapsed_ms"] == 1234.5
    # 时间戳是可解析的 UTC ISO8601
    dt.datetime.fromisoformat(data["ts"])


def test_append_writes_one_json_object_per_line(tmp_path) -> None:
    path = tmp_path / "metrics" / "compaction.jsonl"
    append_compaction_metric(_metric(), path)
    append_compaction_metric(
        _metric(
            success=False,
            after_tokens=None,
            degraded_reason="ContextTooLongError",
            boundary_id="",
        ),
        path,
    )

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["success"] is True and first["after_tokens"] == 120_000
    assert second["success"] is False
    assert second["after_tokens"] is None
    assert second["degraded_reason"] == "ContextTooLongError"


def test_file_observer_appends(tmp_path) -> None:
    path = tmp_path / "compaction.jsonl"
    observer = file_compaction_observer(path)
    observer(_metric())
    observer(_metric())
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_append_never_raises_when_path_unwritable(tmp_path) -> None:
    # 父路径里有一段其实是普通文件：mkdir 会抛 NotADirectoryError，必须被吞掉
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    bad_path = blocker / "sub" / "compaction.jsonl"

    append_compaction_metric(_metric(), bad_path)  # 不抛即通过


# ---------- [D2 构成账] 压前窗口的七桶估算 token 随遥测落盘 ----------


def test_to_dict_includes_composition_defaulting_to_none() -> None:
    data = _metric().to_dict()
    assert "composition" in data
    assert data["composition"] is None


def test_to_dict_carries_composition_and_is_json_serializable() -> None:
    comp = {
        "system": 12,
        "context": 100,
        "user": 9000,
        "assistant": 30,
        "tool_call": 40,
        "artifact": 5000,
        "scaffolding": 15,
    }
    data = _metric(composition=comp).to_dict()
    assert data["composition"] == comp
    json.dumps(data)  # 账本是行分隔 JSON，构成必须可直接序列化


def test_append_persists_composition(tmp_path) -> None:
    path = tmp_path / "metrics" / "compaction.jsonl"
    comp = {
        "system": 12,
        "context": 0,
        "user": 9000,
        "assistant": 0,
        "tool_call": 0,
        "artifact": 5000,
        "scaffolding": 0,
    }
    append_compaction_metric(_metric(composition=comp), path)

    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["composition"] == comp
