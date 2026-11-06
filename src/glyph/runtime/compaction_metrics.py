"""[v2.1 · D2] 上下文压缩遥测：独立计数记录落盘。

设计稿 §12.3：每次 L3 压缩的「级别 / 触发预判水位 / 压后水位 / 写手耗时 / 成败 /
降级原因 / 材料是否截断」要有数据来源，供上线后看压缩频率、降级率、写手耗时曲线
（判断是否需要 map-reduce 重启）。

为守住 B2（runtime 运行时不 import session），这里**不引用** ``session.CompactionReport``，
而是定义 runtime 自己的轻量 DTO :class:`CompactionMetric`；loop 从端口返回的 report
组装它，再交给观察者。落盘用行分隔 JSON（jsonl），追加写、崩溃不影响已有行。

铁律：遥测是旁路，**任何写盘异常都只 warning、绝不抛进主循环**（§12.4）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger("glyph.compaction-metrics")


@dataclass(frozen=True)
class CompactionMetric:
    """一次压缩尝试的遥测样本（与 session.CompactionReport 解耦的 runtime DTO）。"""

    success: bool
    level: str
    before_tokens: int | None
    after_tokens: int | None
    elapsed_ms: float | None
    degraded_reason: str = ""
    material_truncated: bool = False
    boundary_id: str = ""
    # [D2 构成账] 压前窗口的七桶**本地估算 token**（键：system/context/user/
    # assistant/tool_call/artifact/scaffolding）；拿不到时为 None。只落盘给开发者
    # 诊断"窗口被谁占满"，不进用户界面、不发额外请求。
    composition: dict[str, int] | None = None

    def to_dict(self) -> dict:
        return {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "success": self.success,
            "level": self.level,
            "boundary_id": self.boundary_id,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "elapsed_ms": self.elapsed_ms,
            "material_truncated": self.material_truncated,
            "degraded_reason": self.degraded_reason,
            "composition": self.composition,
        }


def append_compaction_metric(metric: CompactionMetric, path: Path) -> None:
    """把一条遥测追加到 jsonl；父目录自动创建，失败只记日志、不抛。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(metric.to_dict(), ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 遥测旁路永不炸主循环
        _LOGGER.warning("压缩遥测落盘失败：%s", path, exc_info=True)


def file_compaction_observer(path: Path) -> Callable[[CompactionMetric], None]:
    """构造一个供 ``AgentLoop(compaction_observer=)`` 使用的同步落盘回调。"""

    def _observe(metric: CompactionMetric) -> None:
        append_compaction_metric(metric, path)

    return _observe
