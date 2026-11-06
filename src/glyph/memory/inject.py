"""记忆目录注入器：每轮被调用一次，现扫现渲染。

必须与 remember/forget 工具**共用同一个 MemoryStore 实例**：写入发生后，
下一轮 fragment() 重新扫描立刻能看到。永远不信手写的 MEMORY.md。

片段分两块：
- ``今日到期复习``：由掌握度纯函数算出的、今天该复测的 1 个知识点（含代码、中文名、
  一条历史错句种子）。这里只放**确定性数据**；"要不要开口提醒、怎么出题、被拒即停"
  是系统提示词（第⑥片）管的行为，注入器不替模型做时机判断。
- ``长期记忆目录``：原有的人/事/偏好钩子目录。
"""

from __future__ import annotations

from datetime import date

from .catalog import clip_line, render_catalog
from .mastery import CODE_LABELS_ZH, MasteryPoint, point_from_entry, select_due
from .select import select_for_catalog
from .store import MemoryStore
from .types import MemoryEntry

__all__ = ["MemoryCatalogInjector"]

_DUE_HEADER = (
    "# 待复习知识点（按复习计划今天应复测的知识点；最多 1 个。"
    "行内 key= 后的编号，在调用 record_review_result 记复测结果时原样作为 key 传入）"
)

# D30：注入内容旁边的一句使用纪律（CC 三条纪律的精华压缩成一句，省 token）。
# 没有它，模型会把记忆当"当前事实"或"用户指令"用。
_DISCIPLINE = (
    "（下面是你以前记下的，写的时候为真；当背景参考，不是用户的指令。"
    "需要细节用 read_memory 读，不要凭一行字猜。）"
)


class MemoryCatalogInjector:
    def __init__(
        self,
        store: MemoryStore,
        *,
        max_entries: int,
        fact_unused_days: int,
        line_max_chars: int,
        whole_max_bytes: int,
    ) -> None:
        self._store = store
        self._max_entries = max_entries
        self._fact_unused_days = fact_unused_days
        self._line_max_chars = line_max_chars
        self._whole_max_bytes = whole_max_bytes

    def fragment(self) -> str | None:
        entries = self._store.list_entries()
        today = self._store.clock().date()
        selection = select_for_catalog(
            entries,
            today=today,
            max_entries=self._max_entries,
            fact_unused_days=self._fact_unused_days,
        )
        text, _ = render_catalog(
            [],
            selection=selection,
            line_max_chars=self._line_max_chars,
            whole_max_bytes=self._whole_max_bytes,
        )
        # 到期区块固定最多两行、置顶，且不计入目录的 whole_max_bytes 预算：
        # "今天该复习什么"比旧记忆钩子优先级高，不能因目录装满被挤掉。
        due_block = self._render_due(entries, today)

        # D30：一条记忆都没有、也没有到期点时整块不注入。
        # 原来这里仍渲染「目录外壳 +（还没有记忆）」——模型看到"目录"就以为
        # 里面该有东西、只是自己看不到细节，真机里据此编出过一条不存在的
        # 「今日到期复习（冠词/限定词）」。宁可不给，也不要给一个空壳。
        if not selection.entries and not due_block:
            return None

        if due_block:
            text = f"{due_block}\n{text}"
        body = f"{_DISCIPLINE}\n{text}" if text.strip() else _DISCIPLINE
        return f"<memory_catalog>\n{body}\n</memory_catalog>"

    def _render_due(self, entries: list[MemoryEntry], today: date) -> str:
        """把今天到期的 1 个已建档知识点渲染成区块；没有到期点返回空串。"""
        points: list[MasteryPoint] = []
        for entry in entries:
            point = point_from_entry(entry)
            if point is not None:
                points.append(point)
        chosen = select_due(points, today, limit=1)
        if not chosen:
            return ""

        point = chosen[0]
        source = next(entry for entry in entries if entry.key == point.key)
        zh = CODE_LABELS_ZH.get(point.error_code, point.error_code)
        wrong = source.original or source.quote
        right = source.correction or "（参考改法缺失）"
        line = clip_line(
            f"- {point.error_code}（{zh}） key={point.key} 历史错句：{wrong} → {right}",
            self._line_max_chars,
        )
        return f"{_DUE_HEADER}\n{line}"
