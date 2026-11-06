

def test_llm_channel_follows_provider_protocol():
    """协议决定走哪条通道：chat 走 Chat 通道，其余走 Responses 通道。"""
    import dataclasses

    from glyph.config import API_PROTOCOL_CHAT, API_PROTOCOL_RESPONSES, load_settings
    from web.runtime import llm_channel_for

    base = load_settings()
    assert llm_channel_for(dataclasses.replace(base, api_protocol=API_PROTOCOL_CHAT)) == "chat"
    assert llm_channel_for(dataclasses.replace(base, api_protocol=API_PROTOCOL_RESPONSES)) == "responses"
    assert llm_channel_for(dataclasses.replace(base, api_protocol="")) == "responses"