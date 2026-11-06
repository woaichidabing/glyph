"""背词状态仓库（后端镜像）。"""

from .paths import study_root
from .store import StudyStore

__all__ = ["StudyStore", "study_root"]
