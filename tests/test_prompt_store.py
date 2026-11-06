"""提示词版本管理：建档 / 新建 / 切换 / 校验 / 装配回落。

守四条：
1. 第一次用会把内置那份存成 v1（幂等，不会覆盖已有版本）；
2. 版本文件缺段 / 空段 → 装配自动回落内置（绝不因为提示词存坏把对话搞挂）；
3. 切到有问题的版本要拦住；
4. 「当前能力」「工具清单」不进版本文件（版本只管静态段）。
"""

import pytest

from glyph import prompt_store as ps
from glyph.agent_prompt import STATIC_SECTIONS, prompt_sections


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GLYPH_PROMPTS", str(tmp_path / "prompts"))
    yield


def test_first_use_seeds_v1_from_builtin():
    assert ps.ensure_seeded(STATIC_SECTIONS) == "v1"
    items = ps.list_versions()
    assert [i.id for i in items] == ["v1"]
    assert ps.active_id() == "v1"
    body = ps.read_version("v1")
    for name, _ in STATIC_SECTIONS:
        assert f"## {name}" in body


def test_seeding_is_idempotent():
    ps.ensure_seeded(STATIC_SECTIONS)
    ps.create_version("第二版", note="测试")
    ps.ensure_seeded(STATIC_SECTIONS)
    assert len(ps.list_versions()) == 2


def test_new_version_copies_current():
    ps.ensure_seeded(STATIC_SECTIONS)
    new_id = ps.create_version("少问一句", note="别老追问")
    assert new_id == "v2"
    assert ps.read_version(new_id) == ps.read_version("v1")


def test_set_active_and_read_back():
    ps.ensure_seeded(STATIC_SECTIONS)
    ps.create_version("第二版")
    ps.set_active("v2")
    assert ps.active_id() == "v2"


def test_switching_to_unknown_version_is_refused():
    ps.ensure_seeded(STATIC_SECTIONS)
    with pytest.raises(ValueError):
        ps.set_active("v9")


def test_missing_section_is_detected():
    ps.ensure_seeded(STATIC_SECTIONS)
    text = ps.read_version("v1").replace("## tone", "## tom")
    problems = ps.validate_text(text, [n for n, _ in STATIC_SECTIONS])
    assert any("缺少段落：tone" in p for p in problems)


def test_assembly_falls_back_when_version_broken():
    ps.ensure_seeded(STATIC_SECTIONS)
    bad = ps.read_version("v1").replace("## identity", "## nope")
    ps._version_path("v1").write_text(bad, encoding="utf-8")
    assert ps.load_active_sections([n for n, _ in STATIC_SECTIONS]) is None
    # 装配仍然能用内置那份跑起来
    secs = prompt_sections([], vision_capable=False, network_capable=False)
    assert any(name == "identity" for name, _ in secs)


def test_assembly_uses_active_version_text():
    ps.ensure_seeded(STATIC_SECTIONS)
    new_id = ps.create_version("改一句")
    body = ps.read_version(new_id).replace("你是 Glyph，用户的英语助手。", "你是 Glyph，改过的身份。")
    ps._version_path(new_id).write_text(body, encoding="utf-8")
    ps.set_active(new_id)
    secs = dict(prompt_sections([], vision_capable=False, network_capable=False))
    assert "改过的身份" in secs["identity"]


def test_dynamic_sections_never_come_from_version_file():
    ps.ensure_seeded(STATIC_SECTIONS)
    body = ps.read_version("v1")
    assert "## ability" not in body
    assert "## tools" not in body

def test_inner_headings_do_not_split_sections():
    """段正文里的「## 小标题」不能被当成段分隔（tone 段就有）。"""
    text = "## tone\n\n# 说话风格\n\n## 你的口吻：自然\n\n像个人。\n\n## identity\n\n你是谁。\n"
    parsed = dict(ps.parse_sections(text, ["tone", "identity"]))
    assert "## 你的口吻：自然" in parsed["tone"]
    assert parsed["identity"].strip() == "你是谁。"


def test_version_tone_wins_over_style_switch():
    """自定义版本写了 tone 就以版本为准，不能被「说话风格」三档覆盖回内置。"""
    ps.ensure_seeded(STATIC_SECTIONS)
    new_id = ps.create_version("改口吻")
    body = ps.read_version(new_id).replace("## 你的口吻：自然", "## 你的口吻：像朋友")
    ps._version_path(new_id).write_text(body, encoding="utf-8")
    ps.set_active(new_id)
    for style in ("natural", "witty", "calm"):
        secs = dict(prompt_sections([], vision_capable=False, network_capable=False, style=style))
        assert "像朋友" in secs["tone"]


def test_non_utf8_version_file_falls_back_instead_of_crashing():
    """版本文件被存成 GBK/UTF-16 时：装配回落内置，不许把对话搞挂。"""
    ps.ensure_seeded(STATIC_SECTIONS)
    ps._version_path("v1").write_bytes("你是 Glyph".encode("gbk"))
    assert ps.load_active_sections([n for n, _ in STATIC_SECTIONS]) is None
    secs = dict(prompt_sections([], vision_capable=False, network_capable=False))
    assert "你是 Glyph" in secs["identity"]


def test_corrupt_index_is_quarantined_not_rebuilt():
    """索引坏了：备份坏文件、报错，但不重建、不覆盖已有版本文件。"""
    ps.ensure_seeded(STATIC_SECTIONS)
    before = ps._version_path("v1").read_text(encoding="utf-8")
    ps._index_path().write_text("{ 不是 json", encoding="utf-8")
    with pytest.raises(ps.PromptStoreError):
        ps.list_versions()
    # v1.md 一个字都没被改
    assert ps._version_path("v1").read_text(encoding="utf-8") == before
    # 坏索引被留档
    assert any(p.name.startswith("index.json.corrupt-") for p in ps._index_path().parent.iterdir())


def test_seed_does_not_overwrite_existing_v1():
    """索引没了但版本文件在：只补索引，不覆盖 v1.md。"""
    ps.ensure_seeded(STATIC_SECTIONS)
    ps._version_path("v1").write_text("## identity\n\n我改过的身份\n", encoding="utf-8")
    ps._index_path().unlink()
    ps.ensure_seeded(STATIC_SECTIONS)
    assert "我改过的身份" in ps._version_path("v1").read_text(encoding="utf-8")


def test_v6_style_version_assembles_without_leftover_placeholders(tmp_path, monkeypatch):
    """用版本文件装配时：占位符必须全部被替换掉，关键细则不能丢。

    审查指出：v6 曾把 {vision_example} 写进 routing 段，而组装只在 speaking 段替换，
    结果系统词里残留字面占位符。这条测试直接盯"装出来是什么"。
    """
    import re

    from glyph.agent_prompt import build_system_prompt

    monkeypatch.setenv("GLYPH_PROMPTS", str(tmp_path / "prompts"))
    ps.ensure_seeded(STATIC_SECTIONS)
    ver = ps.create_version("断言装出来的东西")
    text = ps.read_version(ver)
    # 在 speaking 段放 vision 示例占位符（组装只在那里替换）
    text = text.replace("## speaking", "## speaking\n\n{vision_example}")
    ps._version_path(ver).write_text(text, encoding="utf-8")
    ps.set_active(ver)

    prompt = build_system_prompt(tools=[])
    leftovers = re.findall(r"\{[a-z_]+\}", prompt)
    assert leftovers == [], f"占位符没被替换：{leftovers}"
    assert "我先看一下你之前的学习记录" in prompt or "批改" in prompt
