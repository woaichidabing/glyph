"""Glyph 学习尝试记录层（本地、单学习者、无登录，T07）。

与长期记忆（``glyph.memory``）平级但**职责不同**：

- ``memory`` 是错题 / 事实的长期记忆，其中 ``mastery`` 还维护一套带到期 / 连对 /
  已掌握的掌握度状态机（只覆盖 6 类客观硬语法点）；
- 本包只记**一次短练习的可核对事实**（练了什么知识点、题面、用户实际作答、
  有没有用提示、三档结果），不评分、不推断掌握、不挑知识点类型——中式英语、
  宽泛的"过去时"主题都能记。

一条尝试 = 一个 markdown 文件；frontmatter 存单行标量，正文 ``##`` 分节存
题目 / 作答 / 依据 / 用户确认原话。写入必须经用户明确确认（见 record_attempt 工具）。
"""

from __future__ import annotations

from .errors import (
    AttemptError,
    AttemptNotFoundError,
    AttemptStoreError,
    AttemptValidationError,
)
from .paths import attempt_path, attempts_root, is_valid_attempt_id, trash_dir, trash_path
from .store import (
    DEFAULT_READ_LIMIT,
    DEFAULT_TOTAL_MAX_BYTES,
    AttemptPage,
    AttemptStore,
)
from .types import ATTEMPTS_VERSION, AttemptRecord, Outcome, build_attempt, derive_attempt_id

__all__ = [
    "ATTEMPTS_VERSION",
    "DEFAULT_READ_LIMIT",
    "DEFAULT_TOTAL_MAX_BYTES",
    "AttemptError",
    "AttemptNotFoundError",
    "AttemptPage",
    "AttemptRecord",
    "AttemptStore",
    "AttemptStoreError",
    "AttemptValidationError",
    "Outcome",
    "attempt_path",
    "attempts_root",
    "build_attempt",
    "derive_attempt_id",
    "is_valid_attempt_id",
    "trash_dir",
    "trash_path",
]
