"""grade_text 组装工厂测试（离线）。

工厂负责把"配置 + openai 客户端 + 批改器 + 拼写器"接成一个可注册的工具。
注入假客户端时不联网；不传客户端时工厂自建 AsyncOpenAI（仅构造对象，不发请求）。
"""

import json

from glyph.config import Settings
from glyph.runtime.registry import ToolRegistry
from glyph.tools.factory import build_grade_text_tool, build_polish_text_tool
from glyph.tools.grade import GradeTextTool


def _settings(**over) -> Settings:
    base = dict(
        api_key="k",
        base_url="https://example/v1",
        model="main-model",
        max_tokens=8192,
        max_tokens_upgrade=65536,
        grade_model="grade-model",
        stream_idle_timeout=90.0,
        tool_timeout=120.0,
    )
    base.update(over)
    return Settings(**base)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Resp:
    def __init__(self, content):
        class _Choice:
            def __init__(self):
                self.message = _Msg(content)
                self.finish_reason = "stop"

        self.choices = [_Choice()]


class _Completions:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.resp


class _Chat:
    def __init__(self, completions):
        self.completions = completions


class OneShotClient:
    def __init__(self, payload):
        self.completions = _Completions(_Resp(payload))
        self.chat = _Chat(self.completions)


async def test_factory_wires_grade_model_and_runs_through_registry():
    client = OneShotClient(json.dumps({"detections": [], "summary": "写得不错"}))
    tool = build_grade_text_tool(_settings(), client=client)
    registry = ToolRegistry()
    registry.register(tool)

    result = await registry.execute("grade_text", {"text": "I am happy."})

    assert result.ok is True
    # 批改请求走的是独立的 grade_model，不是主对话模型
    assert client.completions.calls[0]["model"] == "grade-model"
    # 真拼写器跑干净文本，无拼写问题；模型也返回空
    assert result.output["detections"] == []
    assert result.output["summary"] == "写得不错"


def test_factory_builds_its_own_client_when_not_injected():
    tool = build_grade_text_tool(_settings(api_key="k"))

    assert isinstance(tool, GradeTextTool)
    assert tool.name == "grade_text"


async def test_polish_uses_its_own_model_not_grade_model(monkeypatch):
    """润色走**独立的** polish_model，不与批改共用 grade_model。

    2026-10-08 真机 A/B 结论（设计文档 §12）：flash 系在"本来就写得好"的文本上会
    硬挑毛病（谄媚），而 qwen3.7-plus 老实说"没得改"。两者合格线不同，故拆开。
    """
    client = OneShotClient(
        json.dumps({"suggestions": [], "nothing_to_polish": True, "overall": "已经到位"})
    )
    settings = _settings(grade_model="grade-model", polish_model="polish-model")
    tool = build_polish_text_tool(settings, client=client)

    result = await tool.run(
        {"text": "The committee rejected the proposal without giving any reason to the board."}
    )

    # 关键断言：请求里的 model 是 polish_model，**不是** grade_model
    assert client.completions.calls[0]["model"] == "polish-model"
    assert result["nothing_to_polish"] is True


async def test_polish_has_its_own_default_model(monkeypatch):
    """polish_model 有独立默认值，且**不等于** grade_model 的默认值（刻意解耦）。"""
    from glyph.config import DEFAULT_GRADE_MODEL, DEFAULT_POLISH_MODEL

    assert DEFAULT_POLISH_MODEL  # 非空
    assert DEFAULT_POLISH_MODEL != DEFAULT_GRADE_MODEL


def test_load_settings_reads_polish_model_env(monkeypatch):
    """GLYPH_POLISH_MODEL 能覆盖默认值（R1 配置单点）。"""
    from glyph.config import DEFAULT_POLISH_MODEL, load_settings

    monkeypatch.setenv("GLYPH_POLISH_MODEL", "env-polish-model")
    assert load_settings().polish_model == "env-polish-model"

    monkeypatch.delenv("GLYPH_POLISH_MODEL", raising=False)
    assert load_settings().polish_model == DEFAULT_POLISH_MODEL
