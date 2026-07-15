# core/provider_errors.py
"""provider 层业务异常体系 (query_loop 健壮性 spec §3.1)。

adapter 把 HTTP / SSE / transport 错误分类为这三类, query_loop 的 except ProviderError
按 isinstance 分发到责任链错误规则。UserInterruptError 独立 (非 provider 错误, §7)。
"""
from __future__ import annotations


class ProviderError(Exception):
    """provider 业务异常基类。query_loop 的 except ProviderError 接它。"""

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        body: bytes | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body


class TransientProviderError(ProviderError):
    """网络瞬时错误 / 5xx / 429 / overload —— 可重试。"""


class PromptTooLongError(ProviderError):
    """HTTP 400 且 body 指示 prompt 过长。"""


class FatalProviderError(ProviderError):
    """其余 4xx / 流 error 事件 / 未知 —— 不可重试。"""


class UserInterruptError(Exception):
    """用户主动中断。本次定义但不触发; 未来 stream 循环接入 abort_signal 时 raise (§7)。"""
