"""phase: 主动压缩 —— Phase 5 实现,Phase 1 桩(直通)。"""
from __future__ import annotations

from ...types import State
from telemetry.tracer import Tracer


async def maybe_compact(state: State, params, tracer: Tracer) -> State:
    """Phase 1: 直通不压缩。Phase 5: 触发式压缩(超阈值且未试过时摘要历史)。"""
    # Phase5: 触发式压缩
    return state
