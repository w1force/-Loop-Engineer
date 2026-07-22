"""核心数据模型 (P1 §4 改进版)。

QueryState/Message 等仍是 pydantic v2(后续工具入参 schema 用 `.model_json_schema()`)。
AgentState/SkillMeta/Tombstone 是 dataclass(内部状态容器/纯数据,不需校验/序列化)。
"""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from core.file_state import FileStateCache


# ── 文件读状态 ────────────────────────────────────────
# (从 core/builtin_tools/readstate.py 移入:types 反向依赖 builtin_tools 会触发
#  types → builtin_tools/__init__ → tools → types 循环 import;和 SkillMeta 一样自含。)
@dataclass
class ReadRecord:
    content: str
    mtime: float
    offset: int
    limit: int | None


class FileReadState:
    """agent 级文件读状态: read 记录 mtime, write 查陈旧。跨轮持久(不随 ToolContext 重建)。"""

    def __init__(self) -> None:
        self._records: dict[str, ReadRecord] = {}

    def set(self, path: str, content: str, mtime: float,
            offset: int, limit: int | None) -> None:
        self._records[path] = ReadRecord(content, mtime, offset, limit)

    def get(self, path: str) -> ReadRecord | None:
        return self._records.get(path)

    def is_unchanged(self, path: str, offset: int,
                     limit: int | None, disk_mtime: float) -> bool:
        """read 去重: 同 (path, offset, limit) 且 mtime 未变 → True。"""
        rec = self._records.get(path)
        return (rec is not None and rec.offset == offset
                and rec.limit == limit and rec.mtime == disk_mtime)

    def is_stale(self, path: str, disk_mtime: float) -> bool:
        """write 陈旧: 读过且读后被外部改了(disk mtime > 记录) → True。没读过 → False。"""
        rec = self._records.get(path)
        return rec is not None and disk_mtime > rec.mtime


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


@dataclass(frozen=True)
class SkillMeta:
    """一个 skill 的元数据(从 core/skills/loader.py 移入,避免 types→skills 循环依赖)。"""
    name: str            # = 目录名,skill 标识(load_skill 入参)
    description: str     # frontmatter.description,进 system 目录段
    skill_dir: Path      # skill 目录绝对路径
    skill_md: Path       # SKILL.md 绝对路径(= skill_dir / "SKILL.md")


class QueryState(BaseModel):
    """单次 query_loop 内的循环状态(原 State 改名)。

    字段不变:messages/turn_count/recovery 计数/transition。
    后续 Task 2 起 messages 引用 agent_state.messages(单一来源)。

    注:pydantic v2.13 默认对 list 入参做 copy,会切断与 agent_state.messages 的引用。
    故 orchestrator 用 QueryState.model_construct(messages=...) 跳过校验以保引用。
    (ConfigDict(copy_on_model_validation="none") 在 v2 原生已移除,仅 v1 兼容层支持。)
    """
    # FileStateCache 是内部状态容器(非 BaseModel、不参与校验/序列化),
    # 告知 pydantic 放过它的 schema 生成。
    model_config = ConfigDict(arbitrary_types_allowed=True)

    messages: list[Message]
    turn_count: int = 1
    max_output_tokens_recovery_count: int = 0
    max_output_tokens_override: int | None = None
    has_attempted_autocompact: bool = False
    network_retry_count: int = 0
    transition: Continue | Terminal | None = None
    read_file_state: FileStateCache  = field(default_factory=FileStateCache)


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
    file_read_state: FileReadState = field(default_factory=FileReadState)
    cwd: str = ""
    total_input_tokens: int = 0
    total_output_tokens: int = 0


# ── 常量(对齐真实项目 query.ts) ──
MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3  # query.ts:164
ESCALATED_MAX_TOKENS = 64_000  # 占位:按所用模型上限设定,Phase5 校准


@dataclass
class Tombstone:
    """通知下游: turn_id 这一轮的流式 yield 作废(失败, 将重试或终止)。
    下游收到后丢弃该 turn_id 已收的 StreamEvent/AssistantMessage。
    重试/终止判断: 收到 tombstone 后有新轮(turn_id+1)=重试, loop 结束=终止。"""
    turn_id: int
