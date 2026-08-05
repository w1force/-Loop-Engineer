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
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    # 仅用于类型注解; 运行时不 import, 避免 core/providers 反向依赖 config 层
    # (README 约定 config 层 provider 中立)。adapter 在运行时按鸭子类型读取
    # settings.api_key / base_url / debug_sse / use_http_proxy_env 字段。
    from config import Settings

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


def to_anthropic(messages: list[Message], cache_ref_ids: set[str] | None = None) -> list[dict]:
    """内部 Message → Anthropic messages。

    内部 content block 模型本就照 Anthropic 建,直接 model_dump 即可对齐。

    cache_ref_ids(仅缓存感知式 microcompact 启用时非空):给命中的 tool_result 块加
    cache_reference 标记,供服务端按引用匹配缓存 + 应用 cache_edits 删除。
    默认 None → 与原逻辑逐字节一致(不加任何字段)。
    """
    out: list[dict] = []
    for m in messages:
        if m.role == "user":
            content = m.content
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            else:
                blocks: list[dict] = []
                for b in content:
                    d = b.model_dump()
                    if (
                        cache_ref_ids
                        and d.get("type") == "tool_result"
                        and d.get("tool_use_id") in cache_ref_ids
                    ):
                        d["cache_reference"] = d["tool_use_id"]
                    blocks.append(d)
                out.append({"role": "user", "content": blocks})
        else:  # assistant
            # CompactBoundaryMessage(role=="system") 是 LE 内部边界标记,不进入
            # Anthropic messages 数组(对齐 CC normalizeMessagesForAPI 过滤 system compact_boundary)。
            if m.role == "assistant":
                out.append({"role": "assistant", "content": [b.model_dump() for b in m.content]})
    return out


def to_anthropic_tools(tools: list) -> list[ToolDef]:
    return [t.to_schema() if hasattr(t, "to_schema") else t for t in tools]


class AnthropicAdapter(BaseAdapter, Provider):
    # microcompact 时间式的接口门控标记:标识这是 Anthropic message 接口。
    # compact 用鸭子类型读它(getattr(provider, "api_kind", "")),避免反向 import。
    api_kind: str = "anthropic-messages"

    def __init__(
        self,
        settings: Settings,
        *,
        enable_cache_editing: bool = False,
    ):
        # 吃整个 Settings: 以后新增 http 相关配置(代理/超时/重试...)只需扩 Settings 字段,
        # 不必再改 adapter 构造签名。运行时按鸭子类型读字段,不 import config 层
        # (见模块顶部 TYPE_CHECKING 注释),保持 core ⊥ config 的依赖方向。
        api_key = settings.api_key
        base_url = settings.base_url
        debug_sse = settings.debug_sse
        use_http_proxy_env = settings.use_http_proxy_env
        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        super().__init__(
            base_url=base_url.rstrip("/"),
            headers=headers,
            use_http_proxy_env=use_http_proxy_env,
        )
        self._base_url = base_url.rstrip("/")  # 供 is_first_party_anthropic 判定主机
        self._debug_sse = debug_sse  # True 时打印原始 SSE 流(观察流式节奏)
        # 缓存感知式 microcompact 的操作员总开关(对齐 CC 的 CLAUDE_CACHED_MICROCOMPACT 显式 opt-in)。
        # 默认关。即便开了,也必须 base_url 是真 api.anthropic.com 才生效(见 supports_cache_editing)。
        self.enable_cache_editing = enable_cache_editing

    @property
    def is_first_party_anthropic(self) -> bool:
        """base_url 主机是否真为 api.anthropic.com(对齐 CC isFirstPartyAnthropicBaseUrl)。

        指向 DeepSeek / 智谱 等 anthropic 兼容端点时主机不同 → False → 绝不发 cache_edits。
        这是"只有确认在跟真 api.anthropic.com 说话才开"的核心闸门。
        """
        try:
            return urlparse(self._base_url).hostname == "api.anthropic.com"
        except Exception:
            return False

    @property
    def supports_cache_editing(self) -> bool:
        # 对齐 CC:显式 opt-in(enable_cache_editing)+ 真 api.anthropic.com base_url。
        # 模型是否 claude-4.x 由调用侧(microcompact)另查——模型是 per-request、不在适配器上。
        return self.enable_cache_editing and self.is_first_party_anthropic

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
        # 缓存感知式 microcompact:仅当本适配器启用 cache-editing 时,读取要删的 tool_use_id
        # (由 stream_turn 从 agent_state.mc_deleted 透传)。默认关 → cache_edits 恒为 None →
        # to_anthropic 不加 cache_reference、req_body 不加 cache_edits → 与原逻辑逐字节一致。
        cache_edits = opts.get("cache_edits") if self.enable_cache_editing else None
        cache_ref_ids = set(cache_edits) if cache_edits else None
        req_body = {
            "model": model,
            "messages": to_anthropic(messages, cache_ref_ids),
            "system": system,
            "tools": to_anthropic_tools(tools),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if cache_edits:
            # 通知服务端删除这些 tool_use 的缓存结果(不改本地内容 → 保住热缓存前缀)。
            req_body["cache_edits"] = {
                "type": "cache_edits",
                "edits": [{"type": "delete", "tool_use_id": i} for i in cache_edits],
            }
        # ★ 发请求前埋点(P2 §3.4);req_body 进 payload,run.jsonl 可查完整请求。
        # LLM 完整响应(聚合 + LLM_RESPONSE 落盘)由 aggregate_stream 做 —— 它是 provider
        # 无关的统一收口,所有 provider 的 stream 都经此,不必每个 provider 各写一份聚合。
        # 本方法只管"发请求 + 透传事件",职责单一。
        tracer.emit(
            TraceEvent(
                kind=TraceKind.PROVIDER_REQUEST,
                payload={"model": model, "msg_count": len(messages), "req_body": req_body},
            )
        )
        logger.debug("request body: " + json.dumps(req_body, ensure_ascii=False, indent=2))
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
