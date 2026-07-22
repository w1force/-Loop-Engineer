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
        req_body = {
            "model": model,
            "messages": to_anthropic(messages),
            "system": system,
            "tools": to_anthropic_tools(tools),
            "max_tokens": max_tokens,
            "stream": True,
        }
        # ★ 发请求前埋点(P2 §3.4);req_body 进 payload,run.jsonl 可查完整请求
        tracer.emit(
            TraceEvent(
                kind=TraceKind.PROVIDER_REQUEST,
                payload={"model": model, "msg_count": len(messages), "req_body": req_body},
            )
        )
        logger.debug("request body: " + json.dumps(req_body, ensure_ascii=False, indent=2))
        # _record_response 是纯旁路:透传 _raw_events 的所有事件(对外语义不变),
        # 同时聚合出完整 LLM 响应,finally emit 一条 LLM_RESPONSE 落 run.jsonl。
        async for evt in self._record_response(tracer, self._raw_events(req_body, tracer)):
            yield evt

    async def _raw_events(
        self, req_body: dict, tracer: Tracer
    ) -> AsyncIterator[StreamEvent]:
        """发请求 + 解析 SSE + yield StreamEvent;错误分类后抛(供上层 recovery 责任链)。

        从原 stream 抽出,使 stream 能在外层套 _record_response 聚合旁路。
        """
        try:
            async with self.http.stream("POST", "/v1/messages", json=req_body) as r:
                _t0 = time.perf_counter()  # 计时基准(仅 self._debug_sse 用)
                if r.status_code != 200:
                    body = await r.aread()
                    tracer.emit(
                        TraceEvent(
                            kind=TraceKind.PROVIDER_ERROR,
                            payload={
                                "status": r.status_code,
                                "body": body[:500].decode("utf-8", "replace"),
                            },
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

    async def _record_response(
        self, tracer: Tracer, inner: AsyncIterator[StreamEvent]
    ) -> AsyncIterator[StreamEvent]:
        """透传 inner 事件 + 聚合出完整 LLM 响应,finally emit 一条 LLM_RESPONSE。

        纯旁路:不改 inner 的产出与异常语义(异常仍向 query_loop 传播)。
        聚合参考 stream_turn.aggregate_stream(start 建桶/delta 累积/stop 固化),
        放 provider 层仅为日志用(不 import phase 层,避免 core→core.loop 反向依赖)。
        raw_events 只收非-delta 事件(delta 量大,内容已聚合进 blocks)。
        """
        blocks: dict[int, dict] = {}
        raw_events: list[dict] = []
        stop_reason: str | None = None
        usage: dict = {}
        error: dict | None = None
        try:
            async for evt in inner:
                yield evt  # ★ 透传给上层(aggregate_stream),行为不变
                if evt.type == "content_block_start":
                    idx = evt.index
                    if idx is not None:
                        blocks[idx] = dict(evt.block or {})
                    raw_events.append({"type": "content_block_start", "index": idx, "block": evt.block})
                elif evt.type == "content_block_delta":
                    idx = evt.index
                    b = blocks.get(idx) if idx is not None else None
                    if b is None:
                        continue
                    d = evt.delta or {}
                    if "text" in d:
                        b["text"] = b.get("text", "") + d["text"]
                    if "tool_input" in d:
                        b["input_buf"] = b.get("input_buf", "") + d["tool_input"]
                    # delta 不进 raw_events(量大,内容已聚合进 blocks)
                elif evt.type == "content_block_stop":
                    idx = evt.index
                    b = blocks.get(idx) if idx is not None else None
                    if b is not None and b.get("type") == "tool_use":
                        try:
                            b["input"] = json.loads(b.pop("input_buf", "") or "{}")
                        except json.JSONDecodeError:
                            pass  # 截断残缺:保留 input_buf 供日志诊断
                    raw_events.append({"type": "content_block_stop", "index": idx})
                elif evt.type == "message_start":
                    raw_events.append({"type": "message_start", "message": evt.message})
                elif evt.type == "message_delta":
                    d = evt.delta or {}
                    if "stop_reason" in d:
                        stop_reason = d["stop_reason"]
                    if evt.message and "usage" in evt.message:
                        usage = evt.message["usage"]
                    raw_events.append({
                        "type": "message_delta",
                        "delta": evt.delta,
                        "usage": (evt.message or {}).get("usage"),
                    })
                elif evt.type == "message_stop":
                    raw_events.append({"type": "message_stop"})
        except Exception as e:
            # _raw_events 抛出的 ProviderError(或其它异常):记 error 字段后继续向上抛
            error = {"type": type(e).__name__, "message": str(e)}
            raise
        finally:
            tracer.emit(
                TraceEvent(
                    kind=TraceKind.LLM_RESPONSE,
                    payload={
                        "stop_reason": stop_reason,
                        "usage": usage,
                        "blocks": list(blocks.values()),
                        "raw_events": raw_events,
                        "error": error,
                    },
                )
            )

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
