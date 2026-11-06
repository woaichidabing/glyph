"""用户生词本 + 查词历史（本地单文件持久化，给 web / desktop 共用）。"""

from __future__ import annotations

from .paths import GLYPH_WORDBOOK_ROOT_ENV, STATE_FILENAME, state_path, wordbook_root
from .store import (
    MAX_HISTORY,
    MAX_ITEMS,
    MAX_WORD_LENGTH,
    STATE_VERSION,
    WordbookStore,
    validate_word,
)

__all__ = [
    "GLYPH_WORDBOOK_ROOT_ENV",
    "MAX_HISTORY",
    "MAX_ITEMS",
    "MAX_WORD_LENGTH",
    "STATE_FILENAME",
    "STATE_VERSION",
    "WordbookStore",
    "state_path",
    "validate_word",
    "wordbook_root",
]
