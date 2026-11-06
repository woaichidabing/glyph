"""子模型"首选模型没额度/没权限"时自动换同端点模型重试 —— 只包一层 client。

批改（grader）、单条复核（recheck）、写作分析（writing analyzer）三处都通过
``client.chat.completions.create(model=...)`` 调用各自的子模型。与其在三个类里各写一遍
"撞 403 就换模型"，不如在 client 这一层统一兜：调用方代码一行不改，三处一起获得降级能力。

边界（诚实标注）
----------------
- **只对配额/欠费/鉴权类错误换模型**（:func:`glyph.retry.is_quota_or_auth_error`）：
  网络抖动 / 5xx / 429 是"同一个模型等会儿再试"，仍由各调用方既有的同模型重试处理，
  这里不插手，否则会把临时限流误判成"模型挂了"而乱换；
- **只换 model 名，不换 key/base_url**：降级模型必须与首选模型在同一个 OpenAI 兼容端点、
  用同一套凭据（本项目 catalog 里的模型都经同一网关）。换供应商那种降级不在这一层；
- 每个模型只试一次，全部用不了就把**最后一个异常原样抛回**，交给上层人话化 / 让主脑兜底；
- 只代理 ``chat.completions.create``，client 的其余属性（files、images、with_options…）
  原样透传，避免把 openai 客户端重新实现一遍。
"""

from __future__ import annotations

import logging

from ..retry import is_quota_or_auth_error

logger = logging.getLogger("glyph.llm.fallback")


class _CompletionsProxy:
    def __init__(self, inner, primary: str, fallbacks) -> None:
        self._inner = inner
        self._primary = primary
        self._fallbacks = list(fallbacks)

    async def create(self, **kwargs):
        # 首选模型优先取本次 create 显式传入的 model，缺省回落到构造时的 primary。
        requested = kwargs.get("model") or self._primary
        candidates: list[str] = []
        for name in [requested, *self._fallbacks]:
            if name and name not in candidates:  # 去重保序（首选可能恰好等于某个降级）
                candidates.append(name)

        last_exc: Exception | None = None
        for index, model in enumerate(candidates):
            kwargs["model"] = model
            try:
                return await self._inner.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 是否换模型交给判定函数
                last_exc = exc
                # 还有下一个候选、且当前失败是"这个模型用不了（额度/权限）"才换；
                # 最后一个候选、或任何其它错误（网络 / 5xx / 参数错）都直接抛回。
                if index < len(candidates) - 1 and is_quota_or_auth_error(exc):
                    # 留一条可排查的证据：降级是静默发生的，没日志线上就无从确认是否换过模型。
                    logger.warning(
                        "子模型 %s 因额度/鉴权失败（%s），自动降级到 %s 重试",
                        model,
                        str(exc)[:120],
                        candidates[index + 1],
                    )
                    continue
                raise
        # 理论不可达（循环内要么 return 要么 raise），纯防御
        assert last_exc is not None
        raise last_exc


class _ChatProxy:
    def __init__(self, inner, primary: str, fallbacks) -> None:
        self.completions = _CompletionsProxy(inner, primary, fallbacks)


class FallbackChatClient:
    """包一个 OpenAI 兼容异步客户端，仅在 chat.completions.create 上做"换模型重试"。

    被包装的可以是真 ``openai.AsyncOpenAI``，也可以是测试里的假 client（只要它暴露
    ``chat.completions.create``）；其余属性通过 ``__getattr__`` 原样透传。
    """

    def __init__(self, inner, *, primary: str, fallbacks=()) -> None:
        self._inner = inner
        self.chat = _ChatProxy(inner, primary, fallbacks)

    def __getattr__(self, name: str):
        # 除 chat 外的一切属性透传给被包装的真实客户端
        return getattr(self._inner, name)
