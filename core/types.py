"""核心数据模型 (P1 §4 改进版)。

全部 pydantic v2 —— 后续工具入参 schema 要用 `.model_json_schema()`。
"""
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel


# ── 消息块 ──────────────────────────────────────────
class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[TextBlock]   # 收窄: 原 str | list[dict]
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


# ── 用量 ──────────────────────────────────────────────
class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


# ── 消息 ──────────────────────────────────────────────
class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: list[ContentBlock] | str


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[TextBlock | ToolUseBlock]
    model: str | None = None
    stop_reason: str | None = None
    usage: Usage | None = None


Message = UserMessage | AssistantMessage


# ── 统一流式事件(取自 Anthropic SSE 模型,最细粒度) ──
class StreamEvent(BaseModel):
    """统一内部事件。各 provider adapter 负责翻译成这套。"""

    type: Literal[
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    index: int | None = None
    block: dict | None = None  # content_block_start 时
    delta: dict | None = None  # content_block_delta / message_delta 时
    message: dict | None = None  # message_start / message_delta 时


# ── 状态机枚举 ────────────────────────────────────────
class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


class ContinueReason(str, Enum):
    # ── MVP 必需 ──
    NEXT_TURN = "next_turn"
    # ── 网络重试 ──
    NETWORK_RETRY = "network_retry"
    # ── Phase 5 recovery ──
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
    # 砍掉项(对齐真实实现,本计划不做):
    # COLLAPSE_DRAIN_RETRY / STOP_HOOK_BLOCKING / TOKEN_BUDGET_CONTINUATION


class TerminalReason(str, Enum):
    # ── MVP 必需 ──
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    USER_INTERRUPT = "user_interrupt"  # 用户中断(原 ABORTED)
    MODEL_ERROR = "model_error"
    PROMPT_TOO_LONG = "prompt_too_long"  # 恢复链全失败后才到这
    # ── 可选 ──
    BUDGET_EXCEEDED = "budget_exceeded"
    # 砍掉项: IMAGE_ERROR / HOOK_STOPPED / STOP_HOOK_PREVENTED / BLOCKING_LIMIT


class Continue(BaseModel):
    reason: ContinueReason


class Terminal(BaseModel):
    reason: TerminalReason
    error: str | None = None


class State(BaseModel):
    messages: list[Message]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_autocompact: bool = False
    network_retry_count: int = 0
    transition: Continue | Terminal | None = None


# ── 常量(对齐真实项目 query.ts) ──
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3  # query.ts:164
ESCALATED_MAX_TOKENS = 64_000  # 占位:按所用模型上限设定,Phase5 校准


@dataclass
class Tombstone:
    """通知下游: turn_id 这一轮的流式 yield 作废(失败, 将重试或终止)。
    下游收到后丢弃该 turn_id 已收的 StreamEvent/AssistantMessage。
    重试/终止判断: 收到 tombstone 后有新轮(turn_id+1)=重试, loop 结束=终止。"""
    turn_id: int
