"""Tracer 协议 + 占位实现 (P2 §3.2)。

注入式埋点核心原则:
  1. emit 永不抛错、永不阻塞 loop、不改变返回值(内部 try/except 兜底);
  2. fire-and-forget,不进主流程返回值;
  3. NoopTracer(默认,零开销)/ LoggingTracer(本地日志,Phase1 实现)/
     RemoteTracer(上报外部中间件,占位抛错)。
"""
from __future__ import annotations

import logging
from typing import Protocol

from .events import TraceEvent


class Tracer(Protocol):
    def emit(self, event: TraceEvent) -> None: ...
    def child(self, **ctx) -> Tracer: ...  # 派生带额外上下文(如 depth+1)的子 tracer


class NoopTracer:
    """Phase 1 默认。什么都不做,零开销。"""

    def emit(self, event: TraceEvent) -> None:
        pass

    def child(self, **ctx) -> Tracer:
        return self


class LoggingTracer:
    """Phase 1 可选。打到标准 logging,本地开发观察埋点。"""

    def __init__(self, ctx: dict | None = None):
        self._ctx = ctx or {}
        self._seq = 0

    def emit(self, event: TraceEvent) -> None:
        try:
            self._seq += 1
            logging.getLogger("telemetry").info(
                "%s %s",
                event.kind,
                event.model_dump() | self._ctx | {"seq": self._seq},
            )
        except Exception:
            pass  # 埋点永不影响主流程

    def child(self, **ctx) -> Tracer:
        return LoggingTracer({**self._ctx, **ctx})


class RemoteTracer:
    """★ 占位:上报到外部中间件(服务)。中间件方案确认后实现。

    实现要点(将来):内部 asyncio.Queue + 后台 flush task,emit 只入队不阻塞;
    批量 POST 到中间件;失败静默丢弃或本地降级,绝不反压 loop。
    """

    def __init__(self, endpoint: str):
        raise NotImplementedError("RemoteTracer: 等外部埋点中间件方案确认后实现")

    def emit(self, event: TraceEvent) -> None:
        raise NotImplementedError

    def child(self, **ctx) -> Tracer:
        raise NotImplementedError
