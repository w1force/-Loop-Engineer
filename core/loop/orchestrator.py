"""内层 query_loop 主干 (P2 §4.1)。

只负责顺序编排 + while 循环 + state 整体重建,把每步实现细节委托给 phase 函数。
关键点: 每次 continue 整体重建 state;abort 检查在 stream_turn 之后。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Callable

from ..provider import Provider, ToolDef
from ..tools import default_can_use_tool
from ..types import Message, State, StreamEvent, Terminal, TerminalReason
from telemetry.events import TraceEvent, TraceKind
from telemetry.tracer import Tracer

from .phases.compact import maybe_compact
from .phases.execute_tools import execute_tools_phase
from .phases.stream_turn import stream_turn
from .recovery.rules import build_recovery_chain


@dataclass
class QueryParams:
    messages: list[Message]
    system: str | list[dict]
    model: str
    max_tokens: int
    provider: Provider
    abort_signal: asyncio.Event
    tools: list[ToolDef] = field(default_factory=list)
    max_turns: int = 20
    can_use_tool: Callable = default_can_use_tool


def _emit_transition(tracer: Tracer, transition) -> None:
    tracer.emit(
        TraceEvent(
            kind=TraceKind.TRANSITION,
            payload={"reason": transition.reason.value},
        )
    )


async def query_loop(
    params: QueryParams, tracer: Tracer
) -> AsyncIterator[Message | StreamEvent]:
    """内层 agentic loop。yield 消息给外层;完成时 return(附 Terminal 语义)。"""
    state = State(messages=params.messages, turn_count=1)
    chain = build_recovery_chain()

    while True:
        tracer.emit(TraceEvent(kind=TraceKind.TURN_START, turn=state.turn_count))

        # phase 1: 可选主动压缩(Phase 1 桩:直接返回原 state)
        state = await maybe_compact(state, params, tracer)

        # phase 2: 流式调 LLM + 聚合(边聚合边打点)
        outcome = await stream_turn(state, params, tracer)
        for m in outcome.yielded:  # 透传流事件/assistant 消息给外层
            yield m
        if params.abort_signal.is_set():
            _emit_transition(tracer, Terminal(reason=TerminalReason.ABORTED))
            return

        # phase 3: 分叉。needs_follow_up → 执行工具;否则交给责任链决定 Continue/Terminal
        if outcome.needs_follow_up:
            state = await execute_tools_phase(state, outcome, params, tracer)
            if state.turn_count > params.max_turns:
                _emit_transition(tracer, Terminal(reason=TerminalReason.MAX_TURNS))
                return
            _emit_transition(tracer, state.transition)  # NEXT_TURN
            continue

        # 无 tool_use:责任链按序尝试恢复,产出 Continue 或 Terminal
        decision = chain.handle(state, outcome, params, tracer)
        _emit_transition(tracer, decision.transition)
        if isinstance(decision.transition, Terminal):
            return
        state = decision.next_state
        assert state is not None  # Continue 时责任链必给 next_state;Phase 1 此分支不会到
        continue
