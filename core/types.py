"""核心数据模型 (P1 §4 改进版)。

QueryState/Message 等仍是 pydantic v2(后续工具入参 schema 用 `.model_json_schema()`)。
AgentState/SkillMeta/Tombstone 是 dataclass(内部状态容器/纯数据,不需校验/序列化)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from core.file_state import FileStateCache

if TYPE_CHECKING:
    from core.lsp.manager import LSPServerManager


# ── 消息块 ──────────────────────────────────────────
class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(BaseModel):
    """Provider-visible reasoning that must not be mixed into final answer text."""

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: str = ""


class RedactedThinkingBlock(BaseModel):
    """Opaque provider reasoning block retained for protocol round-trips."""

    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str = ""


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
    # 内部稳定锚点(对齐 CC message.uuid):用于 session-memory lastSummarizedMessageId
    # 与 compact 边界切割。发送给 provider 时不依赖此字段。
    uuid: str = Field(default_factory=lambda: str(uuid4()))


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: list[
        TextBlock | ThinkingBlock | RedactedThinkingBlock | ToolUseBlock
    ]
    model: str | None = None
    stop_reason: str | None = None
    usage: Usage | None = None
    # 本轮组装完成的墙钟时间(epoch 秒),供时间式 microcompact 算"距上条 assistant 的空闲"。
    # 可选、默认 None:老消息/恢复链构造的 assistant 无此值时,时间式跳过(算不出 gap)。
    # 仅内部使用,不进 API 请求(to_anthropic 只取 content)。
    created_at: float | None = None
    # 内部稳定锚点(对齐 CC message.uuid)。
    uuid: str = Field(default_factory=lambda: str(uuid4()))


class CompactBoundaryMessage(BaseModel):
    """内部 compact 边界标记。

    作用:标记"边界之前的历史已被摘要替换";provider 适配器必须过滤它,不作为
    Anthropic/OpenAI messages 发送。保留在 transcript / agent_state.messages 中,便于
    后续 compact 的 floor 计算和调试。
    """

    role: Literal["system"] = "system"
    subtype: Literal["compact_boundary"] = "compact_boundary"
    content: str = "Conversation compacted"
    trigger: Literal["auto", "manual"] = "auto"
    pre_tokens: int = 0
    last_pre_compact_message_uuid: str | None = None
    uuid: str = Field(default_factory=lambda: str(uuid4()))


Message = UserMessage | AssistantMessage | CompactBoundaryMessage


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


@dataclass(frozen=True)
class SkillMeta:
    """一个 skill 的元数据(从 core/skills/loader.py 移入,避免 types→skills 循环依赖)。"""
    name: str            # = 目录名,skill 标识(load_skill 入参)
    description: str     # frontmatter.description,进 system 目录段
    skill_dir: Path      # skill 目录绝对路径
    skill_md: Path       # SKILL.md 绝对路径(= skill_dir / "SKILL.md")
    # Learned Skills are frozen when a stage starts. Generic interactive Skills keep
    # these fields empty and retain the historical live-read behavior.
    snapshot_text: str | None = None
    digest: str | None = None


class QueryState(BaseModel):
    """单次 query_loop 内的循环状态(原 State 改名)。

    字段不变:messages/turn_count/recovery 计数/transition。
    后续 Task 2 起 messages 引用 agent_state.messages(单一来源)。

    注:pydantic v2.13 默认对 list 入参做 copy,会切断与 agent_state.messages 的引用。
    故 orchestrator 用 QueryState.model_construct(messages=...) 跳过校验以保引用。
    (ConfigDict(copy_on_model_validation="none") 在 v2 原生已移除,仅 v1 兼容层支持。)
    """
    messages: list[Message]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_reactive_compact: bool = False
    autocompact_consecutive_failures: int = 0
    network_retry_count: int = 0
    transition: Continue | Terminal | None = None


@dataclass
class SessionMemoryState:
    """
    initialized:上下文是否达到过初始化阈值;tokens_at_last_extraction:上次提取时的上下文 token 数
    (算增长量);in_progress:是否有一次后台提取在飞(防并发重复起、供压缩侧等待)。
    """
    initialized: bool = False
    tokens_at_last_extraction: int = 0
    in_progress: bool = False
    # session-memory.md 已覆盖到的消息 uuid。
    last_summarized_message_uuid: str | None = None
    # 上次触发 memory extraction 的消息 uuid,用于统计"自上次更新以来的工具调用数"
    last_memory_message_uuid: str | None = None
    # compact 会推进 generation；较早代次启动的后台 extraction 不得回写过期边界元数据。
    generation: int = 0


@dataclass
class AgentState:
    """跨 submit 的 agent 会话状态(caller 持有)。

    收编原本散落/闭包的数据:messages(跨 submit 累积)、skills、file_read_state、cwd、预算计数。
    tools 不存(走 QueryParams;executor 注册 + stream_turn 发 API)。
    """
    messages: list[Message] = field(default_factory=list)
    skills: list[SkillMeta] = field(default_factory=list)
    # 已通告过的 skill 名(对齐 CC sentSkillNames):skill 目录只作为一条 user 消息
    # 注入历史一次,之后靠此集合去重、不再重发 —— 保持前缀稳定、便于缓存命中。
    sent_skill_names: set[str] = field(default_factory=set)
    # Read/Edit/Write 共用的乐观锁缓存。放在 agent 级,确保跨 submit 持久。
    file_read_state: FileStateCache = field(default_factory=FileStateCache)
    cwd: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # microcompact 缓存感知式(计数式)状态。
    # 仅当 provider 支持 cache-editing 时才被写入/使用;否则恒为空 → 零影响。
    mc_registered: set[str] = field(default_factory=set)   # 已注册的 compactable tool_use_id(去重)
    mc_tool_order: list[str] = field(default_factory=list)  # 注册顺序(算 active 与最旧优先)
    mc_deleted: set[str] = field(default_factory=set)       # 已通过 cache_edits 通知服务端删除的 id
    # session memory(笔记维护)会话级状态
    sm: SessionMemoryState = field(default_factory=SessionMemoryState)
    # 主 agent 持有的 LSP manager。forked AgentState 不复制该运行时对象；
    # fork 仍继承 LSP tool schema，但执行时由 can_use_tool 拒绝。
    lsp_manager: LSPServerManager | None = None


# ── 常量(对齐真实项目 query.ts) ──
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3  # query.ts:164
ESCALATED_MAX_TOKENS = 64_000  # 占位:按所用模型上限设定,Phase5 校准


@dataclass
class Tombstone:
    """通知下游: turn_id 这一轮的流式 yield 作废(失败, 将重试或终止)。
    下游收到后丢弃该 turn_id 已收的 StreamEvent/AssistantMessage。
    重试/终止判断: 收到 tombstone 后有新轮(turn_id+1)=重试, loop 结束=终止。"""
    turn_id: int
