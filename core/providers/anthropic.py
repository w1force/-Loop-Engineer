"""Anthropic adapter (P1 §5.2) —— Phase 1 核心实现。

Anthropic 的 SSE 本就是统一事件模型,adapter 基本只做反序列化 + 透传 +
PROVIDER_REQUEST 埋点。这是选 Anthropic 模型作统一模型的根本原因。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import AsyncIterator

import httpx

from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from ..provider import BaseAdapter, Provider, ToolDef
from ..provider_errors import (
    FatalProviderError,
    PromptTooLongError,
    ProviderError,
    TransientProviderError,
)
from ..types import Message, StreamEvent
from ._sse import parse_sse

ANTHROPIC_VERSION = "2023-06-01"

logger = logging.getLogger("anthropic")

# 统一 StreamEvent 只建模这 6 种内容事件;ping/error 及未来新增类型在 stream 循环里就地处理
_CONTENT_EVENT_TYPES = {
    "message_start",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
}


def to_anthropic(messages: list[Message]) -> list[dict]:
    """内部 Message → Anthropic messages。

    内部 content block 模型本就照 Anthropic 建,直接 model_dump 即可对齐。
    """
    out: list[dict] = []
    for m in messages:
        if m.role == "user":
            content = m.content
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": [b.model_dump() for b in content]})
        else:  # assistant
            out.append({"role": "assistant", "content": [b.model_dump() for b in m.content]})
    return out


def to_anthropic_tools(tools: list) -> list[ToolDef]:
    return [t.to_schema() if hasattr(t, "to_schema") else t for t in tools]


class AnthropicAdapter(BaseAdapter, Provider):
    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com" , debug_sse: bool = False):
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        super().__init__(base_url=base_url.rstrip("/"), headers=headers)
        self._debug_sse = debug_sse  # True 时打印原始 SSE 流(观察流式节奏)

    async def stream(
        self,
        *,
        messages: list[Message],
        system: str | list[dict],
        tools: list[ToolDef],
        model: str,
        max_tokens: int,
        abort_signal,
        tracer: Tracer,
        **opts,
    ) -> AsyncIterator[StreamEvent]:
        # ★ 发请求前埋点(P2 §3.4)
        tracer.emit(
            TraceEvent(
                kind=TraceKind.PROVIDER_REQUEST,
                payload={"model": model, "msg_count": len(messages)},
            )
        )
        req_body = {
            "model": model,
            "messages": to_anthropic(messages),
            "system": system,
            "tools": to_anthropic_tools(tools),
            "max_tokens": max_tokens,
            "stream": True,
        }

        logger.debug("request body: " + json.dumps(req_body, ensure_ascii=False, indent=2))
        try:
            async with self.http.stream("POST", "/v1/messages", json=req_body) as r:
                _t0 = time.perf_counter()  # 计时基准(仅 self._debug_sse 用)
                if r.status_code != 200:
                    body = await r.aread()
                    tracer.emit(
                        TraceEvent(
                            kind=TraceKind.PROVIDER_ERROR,
                            payload={"status": r.status_code, "body": body[:200]},
                        )
                    )
                    raise self._classify_status_error(r.status_code, body)
                async for data in parse_sse(r):  # data: str(见 _sse.py)
                    if self._debug_sse:
                        print(f"[sse +{time.perf_counter() - _t0:6.3f}s] {data}", file=sys.stderr, flush=True)
                    if data == "[DONE]":  # Anthropic 无 [DONE],保险起备
                        break
                    evt = json.loads(data)
                    t = evt.get("type")
                    if t == "ping":
                        continue  # 心跳保活,忽略
                    if t == "error":  # 流中错误:打埋点并抛
                        tracer.emit(
                            TraceEvent(kind=TraceKind.PROVIDER_ERROR, payload=evt)
                        )
                        raise self._classify_stream_error(evt)
                    if t not in _CONTENT_EVENT_TYPES:
                        continue  # 未知事件容错跳过(未来新增类型不至于炸)
                    yield self._to_stream_event(evt)
        except httpx.TransportError as e:
            # 网络中断(ConnectError/ReadTimeout/RemoteProtocolError 等)→ 可重试
            tracer.emit(
                TraceEvent(
                    kind=TraceKind.PROVIDER_ERROR,
                    payload={"transport": type(e).__name__},
                )
            )
            raise TransientProviderError(f"transport error: {e}") from e

    @staticmethod
    def _classify_status_error(status: int, body: bytes) -> ProviderError:
        """HTTP 状态码 + body → 分类异常(供 query_loop 责任链分发)。"""
        text = body.decode("utf-8", errors="replace").lower()
        if status == 429 or status >= 500:
            return TransientProviderError(f"HTTP {status}", status=status, body=body)
        if status == 400 and "prompt is too long" in text:
            return PromptTooLongError("prompt is too long", status=status, body=body)
        return FatalProviderError(f"HTTP {status}", status=status, body=body)

    @staticmethod
    def _classify_stream_error(evt: dict) -> ProviderError:
        """SSE error 事件 → 分类异常(overloaded 可重试,其余致命)。"""
        err = evt.get("error") or {}
        if err.get("type") == "overloaded_error":
            return TransientProviderError(f"stream overloaded: {err}")
        return FatalProviderError(f"stream error: {err}")

    def count_tokens(self, messages: list[Message]) -> int:
        # Phase 1 粗略估算(Phase 5 compact 才真正用到)
        return sum(len(str(m.model_dump())) for m in messages) // 4

    @staticmethod
    def _to_stream_event(evt: dict) -> StreamEvent:
        t = evt.get("type")
        if t == "message_start":
            return StreamEvent(type=t, message=evt.get("message"))
        if t == "content_block_start":
            return StreamEvent(type=t, index=evt.get("index"), block=evt.get("content_block"))
        if t == "content_block_delta":
            # 归一化 Anthropic 增量类型 → 统一 {text} / {tool_input}(供 aggregate_stream)
            delta = evt.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                delta = {"tool_input": delta.get("partial_json", "")}
            elif delta.get("type") == "text_delta":
                delta = {"text": delta.get("text", "")}
            return StreamEvent(type=t, index=evt.get("index"), delta=delta)
        if t == "content_block_stop":
            return StreamEvent(type=t, index=evt.get("index"))
        if t == "message_delta":
            # Anthropic usage 在顶层;映射到 message 字段供 aggregate_stream 读取
            msg = evt.get("message") or {}
            if "usage" in evt:
                msg = {**msg, "usage": evt["usage"]}
            return StreamEvent(type=t, delta=evt.get("delta"), message=msg)
        return StreamEvent(type="message_stop")  # message_stop(未知类型已在 stream 循环经 _CONTENT_EVENT_TYPES 过滤)
