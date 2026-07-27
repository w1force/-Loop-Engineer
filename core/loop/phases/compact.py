"""phase: 主动压缩。

- 短期记忆压缩(microcompact):已实现,委托给 microcompact.run_microcompact
  (时间式 + 缓存感知式,见该模块)。就地更新 agent_state,不重建 QueryState。
- 深层上下文压缩:先试 session-memory keep-window,失败则 Full compact。
"""
from __future__ import annotations

from ...types import QueryState
from telemetry.tracer import Tracer

from .microcompact import run_microcompact
from ...full_compact import full_compact
from ...session_memory import (
    autocompact_threshold,
    context_tokens,
    maybe_session_memory_compact,
)

MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


async def maybe_compact(
    agent_state,            # 跨 submit 累积的历史(microcompact 作用对象)
    state: QueryState,
    params,
    tracer: Tracer,
) -> QueryState:
    """每轮进循环前的压缩钩子。
    """
    run_microcompact(agent_state, params.provider, params.model)
    token_count = context_tokens(agent_state, params.provider)
    if token_count < autocompact_threshold():
        return state
    if (
        state.autocompact_consecutive_failures
        >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
    ):
        return state

    if await maybe_session_memory_compact(agent_state, params.provider):
        return state.model_copy(update={"autocompact_consecutive_failures": 0})

    if await full_compact(
        agent_state,
        params,
        tracer,
        trigger="auto",
        pre_compact_tokens=token_count,
    ):
        return state.model_copy(update={"autocompact_consecutive_failures": 0})

    return state.model_copy(
        update={
            "autocompact_consecutive_failures":
            state.autocompact_consecutive_failures + 1
        }
    )
