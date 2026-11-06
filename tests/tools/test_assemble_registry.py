"""统一装配台账 ``build_default_registry`` 的测试。

聚焦一件容易错的事：系统提示词里声明的「联网能力」必须与**所选型号自身能力**求交，
不能只看全局开关——否则选了不挂平台联网的 flash，提示词仍自称"已启用联网"，
模型会误以为自己能搜、进而编造。
"""

from __future__ import annotations

from dataclasses import replace

from glyph.config import load_settings
from glyph.tools.ask_user import AskUserGateway
from glyph.tools.assemble import build_default_registry
from glyph.tools.dict_repo import DictRepository
from glyph.wordbook import WordbookStore


class _FakeImageStore:
    """装配期不读图，仅满足 ImageProvider 协议。"""

    async def read(self, image_id: str):
        raise AssertionError("装配期不应读取图片")


def _build_prompt(model: str, web_search: bool) -> str:
    settings = replace(load_settings(), model=model, web_search=web_search)
    _, _, prompt = build_default_registry(
        settings,
        image_store=_FakeImageStore(),
        dict_repo=DictRepository(None),
        upload_dirs=[],
        session_id_provider=lambda: None,
        history_provider=lambda: None,
    )
    return prompt


def test_prompt_marks_network_unavailable_for_flash_even_if_switch_on():
    # flash 不挂平台联网：全局开关开着，系统词也必须说「当前不可用」（关键回归）
    prompt = _build_prompt("qwen3.8-flash", web_search=True)
    assert "当前不可用" in prompt


def test_registry_includes_wordbook_proposal_with_injected_store(tmp_path):
    """收词卡必须和 /api/user/wordbook 共用同一个 store，否则卡片次数 / 已收状态会错。"""
    store = WordbookStore(tmp_path)
    registry, _, prompt = build_default_registry(
        load_settings(),
        image_store=_FakeImageStore(),
        dict_repo=DictRepository(None),
        upload_dirs=[],
        session_id_provider=lambda: None,
        history_provider=lambda: None,
        wordbook_store=store,
    )
    tool = next((t for t in registry.tools() if t.name == "propose_wordbook_add"), None)
    assert tool is not None, "注册表里没有 propose_wordbook_add"
    assert tool._store is store
    assert "propose_wordbook_add" in prompt


def test_registry_includes_ask_user_with_injected_gateway():
    """装配必须把**服务层持有的那个**回答通道交给工具——否则用户点了没反应。"""
    gateway = AskUserGateway()
    registry, _, prompt = build_default_registry(
        load_settings(),
        image_store=_FakeImageStore(),
        dict_repo=DictRepository(None),
        upload_dirs=[],
        session_id_provider=lambda: None,
        history_provider=lambda: None,
        ask_gateway=gateway,
    )
    tool = next((t for t in registry.tools() if t.name == "ask_user"), None)
    assert tool is not None, "注册表里没有 ask_user"
    assert tool.gateway is gateway
    # 注册即对模型可见：提示词的工具清单不能漏掉它
    assert "ask_user" in prompt


def test_prompt_marks_network_enabled_for_capable_brain_with_switch_on():
    prompt = _build_prompt("qwen3.8-max", web_search=True)
    assert "已启用" in prompt


def test_prompt_marks_network_unavailable_when_switch_off():
    prompt = _build_prompt("qwen3.8-max", web_search=False)
    assert "当前不可用" in prompt


def test_prompt_tone_follows_configured_speaking_style():
    """装配出的系统提示词，tone 段必须跟随 settings.speaking_style（配置→提示词的接缝）。"""

    def prompt_for(style: str) -> str:
        settings = replace(load_settings(), speaking_style=style)
        _, _, prompt = build_default_registry(
            settings,
            image_store=_FakeImageStore(),
            dict_repo=DictRepository(None),
            upload_dirs=[],
            session_id_provider=lambda: None,
            history_provider=lambda: None,
        )
        return prompt

    natural, witty, calm = prompt_for("natural"), prompt_for("witty"), prompt_for("calm")

    assert "你的口吻：自然" in natural and "开玩笑" not in natural
    assert "你的口吻：风趣" in witty and "开玩笑" in witty
    assert "你的口吻：冷静" in calm
    # 三档都带着同一条说话底线（换皮不换料）
    assert "诚实比好听重要" in witty
    assert "风格只改口吻，不改结论" in calm


def _registry_and_prompt(settings):
    registry, _, prompt = build_default_registry(
        settings,
        image_store=_FakeImageStore(),
        dict_repo=DictRepository(None),
        upload_dirs=[],
        session_id_provider=lambda: None,
        history_provider=lambda: None,
    )
    return registry, prompt


def test_registry_includes_vision_tools_when_vision_model_configured():
    registry, _ = _registry_and_prompt(replace(load_settings(), vision_model="qwen3-vl-flash"))
    names = {t.name for t in registry.tools()}
    assert "ocr_image" in names
    assert "describe_image" in names


def test_registry_omits_vision_tools_when_no_vision_model():
    """自定义纯文本端点没有看图模型：视觉工具不注册、系统提示词也不列，agent 不会空调；
    纯文本批改等其它工具照常注册。"""
    registry, prompt = _registry_and_prompt(replace(load_settings(), vision_model=""))
    names = {t.name for t in registry.tools()}
    assert "ocr_image" not in names
    assert "describe_image" not in names
    assert "ocr_image" not in prompt
    assert "describe_image" not in prompt
    assert "grade_text" in names
