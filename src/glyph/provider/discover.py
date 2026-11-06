"""上游模型发现：向 OpenAI 兼容端点发 ``GET {base_url}/models``，拉回模型 id 清单。

只做只读探测，不落盘、不改运行配置；前端拿到清单后由用户确认，再走
``POST /api/config`` 整体保存。网络 / 鉴权 / 格式问题统一抛 :class:`ProviderDiscoverError`，
带机器可读的 ``kind`` 与人话 ``message``，供 web 层转成给用户看的提示。

三条探测纪律（2026-10-12 xbcl.link 一小时排障的直接教训）：
- **跟随重定向**：端点把 ``/models`` 302 到 ``/v1/models``、http 跳 https 都很常见；
  不跟随的话 3xx 不进任何错误分支，直接拿重定向的 HTML 解析 JSON，报出一条
  「格式不对」的假错误——地址对、Key 对，却把人往错方向引。
- **404 自动补试 ``/v1/models``**：用户常把 BaseURL 填到根（漏 ``/v1``）；两种常见拼法
  都试过仍 404 才报「地址不对」，并把试过的地址列出来——不猜、不藏。
- **上游的错误也是数据（R5 的另一半）**：401/403 时读 body 里的 code + message
  原样带回。上游明明说了 ``INSUFFICIENT_BALANCE``，绝不能吞成一句猜测性的
  「Key 不对」把人引去怀疑密钥。
"""

from __future__ import annotations

import httpx2 as httpx

from .config import normalize_models

# 探测超时：只是一次列表请求，15 秒足够，避免坏地址把界面卡死。
DEFAULT_DISCOVER_TIMEOUT = 15.0

# body 里带这些字样的 401/403 判定为余额不足（大小写不敏感）——这类站把余额检查
# 做在全局，连「列模型」这种不花钱的操作也拦，必须把「Key 是通的」这层说清楚。
_BALANCE_HINTS = ("insufficient_balance", "insufficient", "balance", "arrears")


class ProviderDiscoverError(Exception):
    """拉取上游模型失败；``kind`` 供前端区分提示，``message`` 是可直接展示的人话。"""

    def __init__(self, message: str, *, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _upstream_code_message(resp: httpx.Response) -> tuple[str, str]:
    """尽力读上游 body 里的 code / message（读不到就是空串）。

    兼容两种常见形态：``{"code": ..., "message": ...}`` 与
    ``{"error": {"code": ..., "message": ...}}``（OpenAI 官方形态）。
    """
    try:
        data = resp.json()
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or ""), str(err.get("message") or "")
    return str(data.get("code") or ""), str(data.get("message") or "")


def _quote_upstream(code: str, message: str) -> str:
    parts = [p for p in (code, message) if p]
    return " · ".join(parts)


def _auth_error(resp: httpx.Response) -> ProviderDiscoverError:
    """401/403 的人话：余额不足单独说清「Key 是通的」，其余原样引用上游。"""
    code, message = _upstream_code_message(resp)
    original = _quote_upstream(code, message)
    haystack = f"{code} {message}".lower()
    if original and any(h in haystack for h in _BALANCE_HINTS):
        return ProviderDiscoverError(
            "这个账户余额不足，连「列模型」也被拦了。你的地址和 Key 本身是通的"
            "（已经验证过鉴权，不是填错了）——去服务商后台充值或换个有余额的 Key，"
            f"再点「获取模型」。上游的原话：{original}",
            kind="auth",
        )
    if original:
        return ProviderDiscoverError(
            f"API Key 没通过鉴权。上游的原话：{original}"
            "——对照它检查密钥是否填对、账户有没有模型权限。",
            kind="auth",
        )
    return ProviderDiscoverError(
        "API Key 不对或没有权限，检查密钥是否填对、有没有模型权限",
        kind="auth",
    )


async def fetch_upstream_models(
    base_url: str,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, ...]:
    """请求 ``{base_url}/models`` 并返回清洗去重后的模型 id 元组。

    先按用户填的地址原样请求；404 自动补试一次 ``{base_url}/v1/models``（最常见的
    漏拼形态），两个都 404 才报「地址不对」并列出试过的地址。``client`` 可注入
    （测试传挂了 MockTransport 的 client）；不传则内部按
    :data:`DEFAULT_DISCOVER_TIMEOUT` 自建（跟随重定向）并在结束后关闭。
    """

    base = base_url.strip().rstrip("/")
    headers = {"Authorization": f"Bearer {api_key.strip()}"}

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=DEFAULT_DISCOVER_TIMEOUT, follow_redirects=True)
    try:
        resp: httpx.Response | None = None
        tried: list[str] = []
        candidates = (base + "/models", base + "/v1/models")
        for attempt, candidate in enumerate(candidates):
            try:
                # 跟随重定向逐请求显式开启：注入的测试 client / 自建 client 都生效，
                # 不依赖构造参数（3xx 不跟随 = 假「格式不对」错误的根源）。
                resp = await client.get(candidate, headers=headers, follow_redirects=True)
            except httpx.TimeoutException as exc:
                raise ProviderDiscoverError(
                    "连接超时了，检查下网络，或稍后再试", kind="timeout"
                ) from exc
            except httpx.ConnectError as exc:
                raise ProviderDiscoverError(
                    "连不上这个地址，确认 BaseURL 填到 /v1 这一级、网络能通",
                    kind="connection",
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderDiscoverError(
                    "发请求时出错了，检查 BaseURL 填得对不对", kind="network"
                ) from exc
            tried.append(f"GET {candidate} → {resp.status_code}")
            # 只有第一次 404 才补试 /v1；其余状态当场处理，不做无谓的第二枪。
            if resp.status_code == 404 and attempt == 0:
                continue
            break

        assert resp is not None  # candidates 非空，循环至少跑一次
        status = resp.status_code
        if status in (401, 403):
            raise _auth_error(resp)
        if status == 404:
            raise ProviderDiscoverError(
                "这两个地址都没有模型列表（原样和 /v1 各试了一次）：\n"
                + "\n".join(tried)
                + "\n确认 BaseURL 填到版本号这一级（像 /v1、/api/v3）；"
                "还不行就在设置里手动添加模型名，跳过这一步照样能用。",
                kind="notfound",
            )
        if status >= 400:
            code, message = _upstream_code_message(resp)
            original = _quote_upstream(code, message)
            detail = f"上游服务返回错误（HTTP {status}）"
            if original:
                detail += f"，上游的原话：{original}"
            raise ProviderDiscoverError(detail + "。稍后再试或联系接口方", kind="status")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderDiscoverError(
                "对方返回的不是正常数据，确认这是 OpenAI 兼容的接口地址",
                kind="bad_response",
            ) from exc

        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ProviderDiscoverError(
                "对方返回的模型列表格式不对（缺 data 列表）", kind="bad_response"
            )

        ids: list[str] = []
        for item in data["data"]:
            # 标准形态是 {"id": "xxx"}；也兼容直接给字符串 id 的中转站。
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
            elif isinstance(item, str):
                ids.append(item)
        return normalize_models(ids)
    finally:
        if owns_client:
            await client.aclose()
