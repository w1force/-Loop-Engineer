from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .forked_agent import run_forked_agent
from .tools import CanUseDecision
from .types import (
    AgentState,
    AssistantMessage,
    CompactBoundaryMessage,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

if TYPE_CHECKING:
    from telemetry.tracer import Tracer

    from .loop.orchestrator import QueryParams

logger = logging.getLogger("session_memory")

# 触发阈值
INIT_TOKENS_THRESHOLD = 10_000       # 上下文首次达到此值才开始维护笔记
UPDATE_TOKENS_THRESHOLD = 5_000      # 之后每涨过这么多 token 触发一次
TOOL_CALLS_BETWEEN_UPDATES = 3       # 对齐 CC DEFAULT_SESSION_MEMORY_CONFIG.toolCallsBetweenUpdates
MAX_SECTION_TOKENS = 2_000           # 提示里给模型的单段上限

# session-memory compact 的 keep-window 配置
SM_COMPACT_MIN_TOKENS = 10_000
SM_COMPACT_MIN_TEXT_MESSAGES = 5
SM_COMPACT_MAX_TOKENS = 40_000
SM_COMPACT_DEFAULT_THRESHOLD = 150_000
SM_COMPACT_SUMMARY_SECTION_TOKENS = 2_000

# md格式
DEFAULT_SESSION_MEMORY_TEMPLATE = """# 会话标题
_5-10 字、信息密度高、无废话的会话描述性标题_

# 当前状态
_现在正在做什么?未完成的待办、下一步动作_

# 任务说明
_用户要构建什么?有哪些设计决策或背景_

# 文件与函数
_有哪些关键文件?各自大致含什么、为何相关_

# 工作流
_常跑哪些命令、什么顺序?输出怎么解读_

# 错误与纠正
_遇到过什么错、怎么修的?用户纠正过什么?哪些做法失败、别再试_

# 关键结果
_若用户要过具体产物(答案/表格/文档),在此原样保留_

# 工作日志
_逐步、极简地记录尝试与完成了什么_
"""


def is_enabled() -> bool:
    """默认关:env LOOP_ENGINEER_SESSION_MEMORY=1 才启用(对齐 CC 的 feature gate)。"""
    return os.environ.get("LOOP_ENGINEER_SESSION_MEMORY", "0") == "1"


def get_session_memory_path(cwd: str) -> str:
    """笔记文件路径:<cwd>/.loop-engineer/session-memory.md。"""
    base = Path(cwd or os.getcwd()) / ".loop-engineer"
    return str(base / "session-memory.md")


def setup_session_memory_file(path: str) -> str:
    """确保笔记文件存在(不存在则用模板初始化),返回当前内容。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(DEFAULT_SESSION_MEMORY_TEMPLATE, encoding="utf-8")
        return DEFAULT_SESSION_MEMORY_TEMPLATE
    return p.read_text(encoding="utf-8")


def is_session_memory_empty(content: str) -> bool:
    """内容仍等于模板 = 还没提取过实质内容。"""
    return content.strip() == DEFAULT_SESSION_MEMORY_TEMPLATE.strip()


def build_session_memory_prompt(current_notes: str, notes_path: str) -> str:
    """构造给 fork 的任务 user 消息(对齐 CC buildSessionMemoryUpdatePrompt 的约束)。"""
    return f"""重要:本条消息与这些指令**不属于**真实的用户对话。笔记正文里**不要**出现任何关于
"记笔记 / session notes / 这些更新指令"的字样。

请基于上面的用户对话(**排除**本条记笔记指令、系统提示、CLAUDE.md、以及任何过往会话摘要),
更新会话笔记文件。文件 {notes_path} 的当前内容如下:
<current_notes>
{current_notes}
</current_notes>

你的**唯一任务**:先用 Read 读取 {notes_path},再用 Edit 更新它,然后**停止**(不要调用其它工具)。
可以做多次 Edit(按需更新每个段落)。

编辑铁律:
- 保持文件结构完整:**绝不**修改/删除/新增段落标题(# 开头的行),**绝不**修改/删除标题下的
  斜体 _段落说明_ 行(那是模板指令,原样保留)。
- **只更新每段"斜体说明行之下"的正文**,不要在既有结构之外新增段落或摘要。
- 写**具体、高密度**的内容:文件路径、函数名、错误信息、确切命令、技术细节。
- "关键结果"段落里,原样保留用户要过的完整产物(表格/答案等)。
- 不要写已经在 CLAUDE.md 里的内容;某段没有实质新信息就**留空/不动**,别写"暂无"之类填充。
- 每段控制在约 {MAX_SECTION_TOKENS} token 以内;接近上限就浓缩(淘汰次要细节、保留最关键)。
- **务必更新"当前状态"段**以反映最新工作 —— 这对压缩后的连续性最关键。

用完 Edit 就停,不要继续。只提炼真实用户对话里的信息,绝不从这些记笔记指令里提炼。"""


def make_notes_can_use_tool(notes_path: str):
    """权限函数:只放行"对笔记文件的 Read / Edit"。

    LE 的 Edit 有乐观锁(需先 Read 过),故这里连 Read 一并放行;其余一律拒。
    """
    async def _can(tc: ToolUseBlock) -> CanUseDecision:
        inp = tc.input if isinstance(tc.input, dict) else {}
        if tc.name in ("Read", "Edit") and inp.get("file_path") == notes_path:
            return CanUseDecision(allow=True)
        return CanUseDecision(
            allow=False, reason=f"session memory fork:只允许对 {notes_path} 执行 Read/Edit"
        )

    return _can


def context_tokens(agent_state: AgentState, provider) -> int:
    """当前上下文 token 数(粗估)。用 provider.count_tokens。"""
    try:
        return provider.count_tokens(agent_state.messages)
    except Exception:  # noqa: BLE001 —— 估算失败不该影响主流程
        return 0


def _message_tokens(message: Message) -> int:
    """粗估单条 message token 数(LE 版 estimateMessageTokens)。"""
    try:
        return max(1, len(message.model_dump_json()) // 4)
    except Exception:  # noqa: BLE001
        return 1


def _has_text_blocks(message: Message) -> bool:
    if isinstance(message, AssistantMessage):
        return any(isinstance(b, TextBlock) and bool(b.text) for b in message.content)
    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            return bool(message.content)
        return any(isinstance(b, TextBlock) and bool(b.text) for b in message.content)
    return False


def _has_tool_calls(message: Message) -> bool:
    return isinstance(message, AssistantMessage) and any(
        isinstance(b, ToolUseBlock) for b in message.content
    )


def _has_tool_calls_in_last_assistant_turn(messages: list[Message]) -> bool:
    last = next((m for m in reversed(messages) if isinstance(m, AssistantMessage)), None)
    return bool(last and _has_tool_calls(last))


def _count_tool_calls_since(messages: list[Message], since_uuid: str | None) -> int:
    """统计 since_uuid 之后 assistant tool_use 数(对齐 CC countToolCallsSince)。"""
    count = 0
    found = since_uuid is None
    for message in messages:
        if not found:
            if getattr(message, "uuid", None) == since_uuid:
                found = True
            continue
        if isinstance(message, AssistantMessage):
            count += sum(1 for b in message.content if isinstance(b, ToolUseBlock))
    return count


def should_extract_memory(agent_state: AgentState, provider) -> bool:
    """是否该触发一次笔记提取(对齐 CC shouldExtractMemory)。

    - 初始化门槛:上下文首次达到 INIT_TOKENS_THRESHOLD 才开始(置 initialized)。
    - 更新门槛:距上次提取的上下文增长 ≥ UPDATE_TOKENS_THRESHOLD(token 门槛始终必需)。
    - 工具调用数 OR 自然断点:token 门槛始终必需;然后满足"自上次更新以来 tool_use
      数 ≥ TOOL_CALLS_BETWEEN_UPDATES"或"最后 assistant turn 无 tool_use"之一才触发。
    """
    sm = agent_state.sm
    if sm.in_progress:
        return False
    msgs = agent_state.messages
    if not msgs or not isinstance(msgs[-1], AssistantMessage):
        return False
    current = context_tokens(agent_state, provider)
    if not sm.initialized:
        if current < INIT_TOKENS_THRESHOLD:
            return False
        sm.initialized = True
    if current - sm.tokens_at_last_extraction < UPDATE_TOKENS_THRESHOLD:
        return False
    tool_calls_since_last = _count_tool_calls_since(msgs, sm.last_memory_message_uuid)
    should = (
        tool_calls_since_last >= TOOL_CALLS_BETWEEN_UPDATES
        or not _has_tool_calls_in_last_assistant_turn(msgs)
    )
    if should:
        sm.last_memory_message_uuid = msgs[-1].uuid
    return should


# 持有后台任务引用,防止 fire-and-forget 的 task 被 GC(对齐"一茬接一茬短命 fork"的生命周期)
_pending: set[asyncio.Task] = set()


async def _do_extraction(
    agent_state: AgentState,
    params: "QueryParams",
    tracer: "Tracer",
    generation: int,
) -> None:
    """一次后台提取:建/读笔记 → 构造任务 → 起隔离 fork 用 Edit 改笔记。失败吞掉记日志。"""
    try:
        notes_path = get_session_memory_path(agent_state.cwd)
        current_notes = setup_session_memory_file(notes_path)
        prompt = build_session_memory_prompt(current_notes, notes_path)
        await run_forked_agent(
            parent_agent_state=agent_state,
            parent_params=params,
            task_prompt=prompt,
            tracer=tracer,
            can_use_tool=make_notes_can_use_tool(notes_path),
            max_turns=5,
            propagate_errors=True,
        )
        if agent_state.sm.generation != generation:
            return
        # 记录本次提取时的上下文规模(用于下次的增长门槛)。
        agent_state.sm.tokens_at_last_extraction = context_tokens(agent_state, params.provider)
        # 只有最后 assistant turn 没有工具调用
        # 时才把 md 覆盖边界推进到最后一条消息,避免 compact 切断未闭合工具轮。
        if agent_state.messages and not _has_tool_calls_in_last_assistant_turn(agent_state.messages):
            agent_state.sm.last_summarized_message_uuid = agent_state.messages[-1].uuid
    except Exception as e:  # noqa: BLE001 —— 后台维护失败不该拖垮主 loop
        logger.warning("session memory extraction failed: %s", e)
    finally:
        agent_state.sm.in_progress = False


def maybe_extract_session_memory(
    agent_state: AgentState, params: "QueryParams", tracer: "Tracer"
) -> None:
    """submit 末尾调用:满足条件则**后台 fire-and-forget** 起一次笔记提取(不阻塞返回)。

    默认关(env);不满足触发条件 / 已有一次在飞 → no-op。绝不 await(背景任务)。
    """
    if not is_enabled() or not should_extract_memory(agent_state, provider=params.provider):
        return
    agent_state.sm.in_progress = True  # 先置位,防同一 agent_state 并发重复起
    task = asyncio.create_task(
        _do_extraction(agent_state, params, tracer, agent_state.sm.generation)
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def await_pending_extractions() -> None:
    """等所有在飞的后台提取完成(供单次运行/退出前调用,确保笔记写完;交互式循环通常不需要)。"""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


async def wait_for_pending_extractions(timeout: float = 15.0) -> None:
    """compact 前等待在飞 memory extraction。

    超时不取消后台任务:继续用当前 md 内容 compact / 或放弃 compact。
    """
    if not _pending:
        return
    await asyncio.wait(list(_pending), timeout=timeout)


# ── session-memory compact 本体 ──────────────────────────────────────────

def autocompact_threshold() -> int:
    raw = os.environ.get("LOOP_ENGINEER_AUTOCOMPACT_TOKENS")
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return SM_COMPACT_DEFAULT_THRESHOLD


def _is_compact_boundary(message: Message) -> bool:
    return isinstance(message, CompactBoundaryMessage)


def _truncate_session_memory_for_compact(content: str) -> tuple[str, bool]:
    """按 section 截断 md,避免 summary message 自身过大。"""
    max_chars = SM_COMPACT_SUMMARY_SECTION_TOKENS * 4
    out: list[str] = []
    section_header = ""
    section_lines: list[str] = []
    was_truncated = False

    def flush() -> None:
        nonlocal was_truncated
        if not section_header:
            out.extend(section_lines)
            return
        section_content = "\n".join(section_lines)
        if len(section_content) <= max_chars:
            out.append(section_header)
            out.extend(section_lines)
            return
        out.append(section_header)
        used = 0
        for line in section_lines:
            if used + len(line) + 1 > max_chars:
                break
            out.append(line)
            used += len(line) + 1
        out.append("\n[... section truncated for length ...]")
        was_truncated = True

    for line in content.split("\n"):
        if line.startswith("# "):
            flush()
            section_header = line
            section_lines = []
        else:
            section_lines.append(line)
    flush()
    return "\n".join(out), was_truncated


def build_compact_summary_message(session_memory: str, notes_path: str) -> UserMessage:
    """把 md 作为 compact 后的 user summary message"""
    truncated, was_truncated = _truncate_session_memory_for_compact(session_memory)
    content = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        f"{truncated}\n\n"
        "Recent messages are preserved verbatim.\n\n"
        "Continue the conversation from where it left off without asking the user any further "
        "questions. Resume directly — do not acknowledge the summary, do not recap what was "
        "happening, do not preface with \"I'll continue\" or similar. Pick up the last task as if "
        "the break never happened."
    )
    if was_truncated:
        content += f"\n\nSome session memory sections were truncated for length. Full notes: {notes_path}"
    return UserMessage(content=content)


def _adjust_index_to_preserve_tool_pairs(messages: list[Message], start_index: int) -> int:
    """向前扩 start_index,确保保留区中的 tool_result 不会失去对应 tool_use。"""
    if start_index <= 0 or start_index >= len(messages):
        return start_index
    adjusted = start_index
    result_ids: list[str] = []
    for message in messages[start_index:]:
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            result_ids.extend(
                b.tool_use_id for b in message.content if isinstance(b, ToolResultBlock)
            )
    if not result_ids:
        return adjusted

    kept_tool_use_ids: set[str] = set()
    for message in messages[adjusted:]:
        if isinstance(message, AssistantMessage):
            kept_tool_use_ids.update(
                b.id for b in message.content if isinstance(b, ToolUseBlock)
            )
    needed = {i for i in result_ids if i not in kept_tool_use_ids}
    for i in range(adjusted - 1, -1, -1):
        if not needed:
            break
        message = messages[i]
        if not isinstance(message, AssistantMessage):
            continue
        ids_here = {b.id for b in message.content if isinstance(b, ToolUseBlock)}
        if ids_here & needed:
            adjusted = i
            needed -= ids_here
    return adjusted


def calculate_messages_to_keep_index(
    messages: list[Message], last_summarized_index: int
) -> int:
    """计算 compact 后原文保留窗口起点。"""
    if not messages:
        return 0
    start = last_summarized_index + 1 if last_summarized_index >= 0 else len(messages)
    total_tokens = sum(_message_tokens(m) for m in messages[start:])
    text_count = sum(1 for m in messages[start:] if _has_text_blocks(m))

    if total_tokens >= SM_COMPACT_MAX_TOKENS:
        return _adjust_index_to_preserve_tool_pairs(messages, start)
    if total_tokens >= SM_COMPACT_MIN_TOKENS and text_count >= SM_COMPACT_MIN_TEXT_MESSAGES:
        return _adjust_index_to_preserve_tool_pairs(messages, start)

    last_boundary = -1
    for i in range(len(messages) - 1, -1, -1):
        if _is_compact_boundary(messages[i]):
            last_boundary = i
            break
    floor = last_boundary + 1 if last_boundary >= 0 else 0

    for i in range(start - 1, floor - 1, -1):
        msg = messages[i]
        total_tokens += _message_tokens(msg)
        text_count += 1 if _has_text_blocks(msg) else 0
        start = i
        if total_tokens >= SM_COMPACT_MAX_TOKENS:
            break
        if total_tokens >= SM_COMPACT_MIN_TOKENS and text_count >= SM_COMPACT_MIN_TEXT_MESSAGES:
            break
    return _adjust_index_to_preserve_tool_pairs(messages, start)


async def maybe_session_memory_compact(agent_state: AgentState, provider) -> bool:
    """autocompact 阈值触发 → 用 session-memory.md 替换旧 messages。

    返回 True 表示已原地改写 agent_state.messages:
    [CompactBoundaryMessage, summary UserMessage, messagesToKeep...]
    """
    if not is_enabled():
        return False
    token_count = context_tokens(agent_state, provider)
    threshold = autocompact_threshold()
    if token_count < threshold:
        return False

    await wait_for_pending_extractions()

    notes_path = get_session_memory_path(agent_state.cwd)
    try:
        #存在则根据path read，不存在则创建write新的
        session_memory = setup_session_memory_file(notes_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("session memory compact: failed to read notes: %s", e)
        return False
    if is_session_memory_empty(session_memory):
        return False

    last_uuid = agent_state.sm.last_summarized_message_uuid
    if last_uuid:
        last_idx = next(
            (i for i, m in enumerate(agent_state.messages) if getattr(m, "uuid", None) == last_uuid),
            -1,
        )
        if last_idx == -1:
            logger.debug("session memory compact: summarized uuid not found, fallback")
            return False
    else:
        # resumed / compact 后尚未重新 extraction:md 有内容但没有明确边界。
        # 先假定不保留任何后缀,再由 keep-window minimum 向前扩近期上下文。
        last_idx = len(agent_state.messages) - 1

    start_idx = calculate_messages_to_keep_index(agent_state.messages, last_idx)
    messages_to_keep = [
        m for m in agent_state.messages[start_idx:] if not _is_compact_boundary(m)
    ]
    boundary = CompactBoundaryMessage(
        trigger="auto",
        pre_tokens=token_count,
        last_pre_compact_message_uuid=getattr(agent_state.messages[-1], "uuid", None)
        if agent_state.messages
        else None,
    )
    summary = build_compact_summary_message(session_memory, notes_path)
    post_compact: list[Message] = [boundary, summary, *messages_to_keep]

    post_tokens = sum(_message_tokens(m) for m in post_compact)
    if post_tokens >= threshold:
        logger.debug(
            "session memory compact: post tokens %d >= threshold %d, fallback",
            post_tokens,
            threshold,
        )
        return False

    # 原地替换,保持 QueryState.messages 与 agent_state.messages 的同一 list 引用。
    agent_state.messages[:] = post_compact
    # compact 后旧 summarized uuid 已被裁掉,重置锚点;
    # 后续 memory extraction 成功后再推进。
    agent_state.sm.generation += 1
    agent_state.sm.last_summarized_message_uuid = None
    return True
