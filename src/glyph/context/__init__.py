"""每轮上下文注入的通用插槽（不持有任何业务内容）。"""

from .attachments import attachments_fragment, render_attachments_context
from .inject import (
    CONTEXT_DISCLAIMER,
    CONTEXT_HEADER,
    ContextFragment,
    env_date_fragment,
    render_context_message,
)
from .internal_messages import CONTINUATION_PROMPT, is_internal_user_message
from .study import StudyContext, render_study_context, study_fragment

__all__ = [
    "CONTEXT_HEADER",
    "CONTEXT_DISCLAIMER",
    "ContextFragment",
    "env_date_fragment",
    "render_context_message",
    "attachments_fragment",
    "render_attachments_context",
    "CONTINUATION_PROMPT",
    "is_internal_user_message",
    "StudyContext",
    "render_study_context",
    "study_fragment",
]
