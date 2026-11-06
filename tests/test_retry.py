"""错误可重试判定测试（离线）。

为什么要单独立一份测试：这条规则以前被抄了三份（llm/qwen.py、llm/responses.py、
tools/grader.py），其中两份写法相同且藏同一个坑 —— ``status_code`` 属性存在但值为
None 时，``None >= 500`` 抛 TypeError，判定函数反而把调用方搞崩。现在合并到
``markr/retry.py`` 一处，这里把「哪些该重、哪些不该重、None 不崩」钉死，
防止有人改回去或再抄一份出去。
"""

import pytest

from glyph.llm.base import LLMError
from glyph.retry import is_quota_or_auth_error, is_retryable


class _FakeHTTPError(Exception):
    """最朴素的假异常：只带一个 status_code，模拟各路客户端/测试替身。"""

    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status_code = status


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_retryable_statuses(status):
    assert is_retryable(_FakeHTTPError(status)) is True


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 422, 499])
def test_non_retryable_statuses(status):
    """4xx 尤其 401（密钥错）重试一万次也是同一个错，不该自愈掩盖。"""
    assert is_retryable(_FakeHTTPError(status)) is False


@pytest.mark.parametrize("status", [None, "500", "", 0.5])
def test_non_integer_status_is_not_retryable_and_does_not_crash(status):
    """回归：status_code 存在但不是 int（最常见的就是 None）时，
    必须老实返回 False，绝不能抛 TypeError 把调用方带崩。"""
    assert is_retryable(_FakeHTTPError(status)) is False


def test_exception_without_status_code_is_not_retryable():
    """完全没有 status_code 属性（普通异常）：无从判断，按不可重试处理。"""
    assert is_retryable(RuntimeError("boom")) is False


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("reset"),
        ConnectionResetError("reset"),
        BrokenPipeError("pipe"),
        TimeoutError("timed out"),
    ],
)
def test_builtin_connection_errors_are_retryable(exc):
    """Python 内置的连接 / 超时异常也算可重试，与 openai 的包装异常同待遇。

    回归（2026-10-14）：讲题工具真机拿到裸 ConnectionError("reset") —— 部分客户端
    （自建 transport / 流式读取中途掉线）不走 openai 包装，原始异常直接冒上来。
    以前这种一律判"不可重试"，于是"同一类掉线"会因为从哪一层冒出来而得到两种待遇。
    """
    assert is_retryable(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        OSError("disk full"),
        ValueError("bad value"),
        FileNotFoundError("no such file"),
    ],
)
def test_local_os_errors_are_not_retryable(exc):
    """只收 ConnectionError / TimeoutError 这两支，不许顺手上收到 OSError。

    FileNotFoundError（本地文件没了）重试一万次也还是不在，把它算成网络抖动会让
    工具对着同一个错反复干等；这里钉住"别为了修掉上面那条而放宽过头"。
    """
    assert is_retryable(exc) is False


def test_connection_errors_are_retryable():
    """连接失败 / 超时可重试。openai 是可选依赖，装了才测。"""
    openai = pytest.importorskip("openai")

    class Boom(openai.APIConnectionError):
        def __init__(self):
            super().__init__(message="connect failed", request=None)

    class Slow(openai.APITimeoutError):
        def __init__(self):
            # APITimeoutError 的 __init__ 只收 request（消息是内置写死的）
            super().__init__(request=None)

    assert is_retryable(Boom()) is True
    assert is_retryable(Slow()) is True


# ---------- is_quota_or_auth_error：额度 / 欠费 / 鉴权（该换模型，不是对同模型干等） ----------


@pytest.mark.parametrize("status", [401, 402, 403])
def test_quota_or_auth_statuses(status):
    assert is_quota_or_auth_error(_FakeHTTPError(status)) is True


@pytest.mark.parametrize("status", [200, 400, 404, 422, 429, 500, 503])
def test_non_quota_statuses(status):
    """400 参数错 / 429 临时限流 / 5xx 临时故障，都不该触发"换模型"。"""
    assert is_quota_or_auth_error(_FakeHTTPError(status)) is False


@pytest.mark.parametrize(
    "text",
    [
        "Free quota exhausted",
        "insufficient balance, please add funds",
        "Invalid API key",
        "forbidden: free tier only",
        "额度已用完",
        "账户欠费，请充值",
    ],
)
def test_quota_hints_in_text_without_status(text):
    """没有状态码、只给错误文案时，命中额度 / 欠费 / 鉴权线索也要认。"""
    assert is_quota_or_auth_error(RuntimeError(text)) is True


def test_quota_judge_does_not_crash_on_none_status():
    """status_code=None 时老实返回 False，不抛 TypeError。"""
    assert is_quota_or_auth_error(_FakeHTTPError(None)) is False


# ---------- 包装后不能丢结论 + 平台真实文案的多种写法（2026-10-12 真机 401 回归） ----------


def test_wrapped_llm_error_keeps_quota_classification():
    """适配层会把原始异常包成 LLMError，状态码在那一层就没了。

    包装时算好的"是不是额度 / 鉴权错"必须跟着一起带上来：这条结论是主循环决定
    "换个模型再试"的唯一依据，丢了它用户就只剩一段英文报错可看。
    """
    assert (
        is_quota_or_auth_error(LLMError("模型调用失败", retryable=False, quota_or_auth=True))
        is True
    )


def test_wrapped_llm_error_without_flag_still_judges_by_text():
    """没带标记时仍然退回文字判定（两条路都在，不是二选一）。"""
    assert is_quota_or_auth_error(LLMError("Free quota exhausted", retryable=False)) is True


@pytest.mark.parametrize(
    "text",
    [
        # 真机样本：千问兼容端点钥匙不对时返回的原文
        "Error code: 401 - {'code': 'InvalidApiKey', 'message': "
        "'Invalid API-key provided.', 'request_id': '20c92e0f'}",
        "InvalidApiKey",
        "Invalid API-key provided.",
        "invalid_api_key",
    ],
)
def test_platform_invalid_key_spellings_are_recognized(text):
    """平台把同一个错写成 Invalid API key / Invalid API-key / InvalidApiKey 三种形态。

    分隔符不同不该改变结论——少一个空格就判不出来，正是那次 401 没触发换脑的原因。
    """
    assert is_quota_or_auth_error(RuntimeError(text)) is True
