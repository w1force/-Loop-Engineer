"""phase: 主动压缩 —— Phase 5 实现,Phase 1 桩(直通)。

Task 2 起 maybe_compact 签名加 agent_state(本期预留,内部不用)。
Phase 5 触发式压缩将基于 agent_state.messages 跨 submit 累积的历史。
"""
from __future__ import annotations

from ...types import QueryState
from telemetry.tracer import Tracer


async def maybe_compact(
    agent_state,            # ★ Task 2 新增(本期内部不用,Phase 5 用 agent_state.messages)
    state: QueryState,
    params,
    tracer: Tracer,
) -> QueryState:
    """Phase 1: 直通不压缩。Phase 5: 触发式压缩(用 agent_state.messages)。"""
    # Phase5: 触发式压缩
    return state
