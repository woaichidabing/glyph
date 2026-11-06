"""上游模型发现（GET {base_url}/models）测试：用 httpx MockTransport，绝不打真网络。"""

import httpx2 as httpx
import pytest

from glyph.provider.discover import ProviderDiscoverError, fetch_upstream_models


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_returns_cleaned_deduped_ids() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "gpt-4o-mini"},
                    {"id": ""},  # 空 id 丢
                    {"id": "gpt-4o"},  # 重复去重
                    {"id": "   "},  # 纯空白丢
                    {"id": 123},  # 非字符串丢
                ]
            },
        )

    async with _client(handler) as client:
        models = await fetch_upstream_models("https://api.x.com/v1/", "sk-x", client=client)

    assert models == ("gpt-4o", "gpt-4o-mini")
    # BaseURL 尾斜杠被剥掉再拼 /models；鉴权头标准 Bearer
    assert seen["path"] == "/v1/models"
    assert seen["auth"] == "Bearer sk-x"


async def test_fetch_empty_list_returns_empty_tuple() -> None:
    async with _client(lambda r: httpx.Response(200, json={"data": []})) as client:
        assert await fetch_upstream_models("https://x/v1", "k", client=client) == ()


async def test_fetch_strips_whitespace_in_url_and_key() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    async with _client(handler) as client:
        models = await fetch_upstream_models("  https://x/v1/  ", "  sk-k  ", client=client)

    assert models == ("m1",)
    assert seen["path"] == "/v1/models"
    assert seen["auth"] == "Bearer sk-k"


async def test_fetch_401_is_auth_error() -> None:
    async with _client(lambda r: httpx.Response(401, json={"error": "bad"})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "auth"


async def test_fetch_403_is_auth_error() -> None:
    async with _client(lambda r: httpx.Response(403, json={})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "auth"


# ---------- 三条探测纪律（2026-10-12 xbcl.link 排障教训的固化） ----------


async def test_fetch_404_autoretries_v1_models_once() -> None:
    """用户填了根地址（漏 /v1）：第一个 404 后自动补试 /v1/models 并成功。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/models":
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    async with _client(handler) as client:
        models = await fetch_upstream_models("https://x.com", "k", client=client)
    assert models == ("m1",)
    assert seen == ["/models", "/v1/models"]


async def test_fetch_both_404_reports_tried_addresses() -> None:
    """两种拼法都 404 才报地址不对，且把试过的地址原样列出——不猜、不藏。"""
    async with _client(lambda r: httpx.Response(404, json={})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x.com", "k", client=client)
    assert ei.value.kind == "notfound"
    assert "GET https://x.com/models → 404" in ei.value.message
    assert "GET https://x.com/v1/models → 404" in ei.value.message


async def test_fetch_non_404_error_does_not_retry() -> None:
    """401 不触发补试（鉴权问题换路径也没用），原样一次。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(401, json={"code": "INVALID_KEY", "message": "bad key"})

    async with _client(handler) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x.com", "k", client=client)
    assert seen == ["/models"]
    assert ei.value.kind == "auth"
    assert "INVALID_KEY · bad key" in ei.value.message


async def test_fetch_insufficient_balance_says_key_is_fine() -> None:
    """xbcl.link 一小时排障的反面教材：上游明明说余额不足，不能吞成「Key 不对」。"""
    async with _client(
        lambda r: httpx.Response(
            403,
            json={"code": "INSUFFICIENT_BALANCE", "message": "Insufficient account balance"},
        )
    ) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://xbcl.link", "k", client=client)
    assert ei.value.kind == "auth"
    assert "余额不足" in ei.value.message
    assert "地址和 Key 本身是通的" in ei.value.message
    assert "INSUFFICIENT_BALANCE · Insufficient account balance" in ei.value.message


async def test_fetch_500_includes_upstream_quote() -> None:
    async with _client(
        lambda r: httpx.Response(500, json={"code": "BOOM", "message": "internal"})
    ) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "status"
    assert "BOOM · internal" in ei.value.message


async def test_fetch_follows_redirects() -> None:
    """端点把 /models 302 到 /v1/models：跟随重定向，而不是拿 HTML 解析出假错误。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/models":
            return httpx.Response(302, headers={"location": "https://x.com/v1/models"})
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    async with _client(handler) as client:
        models = await fetch_upstream_models("https://x.com", "k", client=client)
    assert models == ("m1",)
    assert seen == ["/models", "/v1/models"]


async def test_fetch_404_is_notfound_error() -> None:
    async with _client(lambda r: httpx.Response(404, json={})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "notfound"


async def test_fetch_500_is_status_error() -> None:
    async with _client(lambda r: httpx.Response(500, json={})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "status"


async def test_fetch_connect_error_is_reported_as_connection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns fail", request=request)

    async with _client(handler) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "connection"


async def test_fetch_timeout_is_reported_as_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    async with _client(handler) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "timeout"


async def test_fetch_non_json_body_is_bad_response() -> None:
    async with _client(
        lambda r: httpx.Response(200, text="oops", headers={"content-type": "text/html"})
    ) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "bad_response"


async def test_fetch_data_not_a_list_is_bad_response() -> None:
    async with _client(lambda r: httpx.Response(200, json={"data": "nope"})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "bad_response"

    async with _client(lambda r: httpx.Response(200, json={"models": []})) as client:
        with pytest.raises(ProviderDiscoverError) as ei:
            await fetch_upstream_models("https://x/v1", "k", client=client)
    assert ei.value.kind == "bad_response"
