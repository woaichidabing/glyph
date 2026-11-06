"""附件上下文片段：只陈述事实，不替用户宣布意图。"""

from glyph.context.attachments import attachments_fragment, render_attachments_context


def test_none_and_empty_return_none():
    assert render_attachments_context(None) is None
    assert render_attachments_context([]) is None


def test_renders_facts_not_instructions():
    text = render_attachments_context([{"filename": "essay.jpg", "image_id": "abc"}])
    assert text
    assert "essay.jpg" in text
    assert "abc" in text
    assert "不是用户原话" in text
    assert "批改" not in text
    assert text.startswith("<uploaded_files>")
    assert text.endswith("</uploaded_files>")


def test_fragment_follows_getter():
    items = [{"filename": "a.jpg", "image_id": "1"}]
    frag = attachments_fragment(lambda: items)
    assert "a.jpg" in (frag() or "")
    items.clear()
    assert frag() is None
