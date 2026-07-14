"""结构化埋点事件定义 (P2 §3.3)。

用一个 `kind` 字段区分事件类型,`payload` 放差异字段。
Phase 1 真正用到的: TURN_START / PROVIDER_REQUEST / TOOL_USE_DETECTED /
STREAM_END / TRANSITION;其余枚举先定义占位,后续 phase 再打。
"""
from enum import Enum

from pydantic import BaseModel, Field


class TraceKind(str, Enum):
    # ── loop 生命周期 ──
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    TRANSITION = "transition"  # 每次 Continue/Terminal,带 reason
    # ── 流式过程(细粒度,深入 aggregate_stream) ──
    STREAM_START = "stream_start"
    TOOL_USE_DETECTED = "tool_use_detected"  # ★ 流式中识别到 LLM 想调用某 tool
    TEXT_DELTA = "text_delta"  # 可选,量大,默认采样/关闭
    STREAM_END = "stream_end"
    # ── 工具执行 ──
    TOOL_EXEC_START = "tool_exec_start"
    TOOL_EXEC_END = "tool_exec_end"
    # ── 恢复 ──
    RECOVERY_ATTEMPT = "recovery_attempt"  # 命中某条 TransitionRule
    # ── provider ──
    PROVIDER_REQUEST = "provider_request"
    PROVIDER_ERROR = "provider_error"


class TraceEvent(BaseModel):
    kind: TraceKind
    chain_id: str | None = None
    depth: int = 0
    turn: int | None = None
    payload: dict = Field(default_factory=dict)
