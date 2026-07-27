"""CC 风格的 Full compact:fork 生成全量摘要并替换运行时上下文。"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .builtin_tools.read import read
from .forked_agent import ForkedAgentError, run_forked_agent
from .provider_errors import PromptTooLongError
from .tools import CanUseDecision
from .types import (
    AgentState,
    AssistantMessage,
    CompactBoundaryMessage,
    Message,
    TerminalReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from telemetry.events import TraceEvent, TraceKind

if TYPE_CHECKING:
    from telemetry.tracer import Tracer

    from .loop.orchestrator import QueryParams

logger = logging.getLogger("full_compact")

COMPACT_MAX_OUTPUT_TOKENS = 20_000
MAX_PROMPT_TOO_LONG_RETRIES = 3
POST_COMPACT_MAX_FILES = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_FILES_TOKEN_BUDGET = 50_000
_PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"

_NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use Read, Bash, Grep, Glob, Edit, Write, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be rejected and will waste your only turn.
- Your response must be an <analysis> block followed by a <summary> block.

"""

_BASE_COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
The summary must preserve the technical details and decisions needed to continue the work without access to the removed conversation.

Before the final summary, use <analysis> tags as a private drafting section. Analyze the conversation chronologically and verify that you captured:
- every explicit user request and correction;
- approaches taken, decisions, code patterns, and technical concepts;
- files, functions, commands, edits, and important code snippets;
- errors encountered, failed approaches, and their fixes;
- unfinished work and the exact state of the most recent task.

Write the final result inside <summary> tags with these sections:
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and Fixes
5. Problem Solving
6. All User Messages
7. Pending Tasks
8. Current Work
9. Optional Next Step

For "All User Messages", list every user message that is not a tool result. For the optional next step, only include work directly implied by the latest explicit request; quote the latest relevant request verbatim when useful.

REMINDER: do not call tools. Return plain text only: <analysis>...</analysis><summary>...</summary>.
"""


def build_full_compact_prompt(custom_instructions: str | None = None) -> str:
    prompt = _NO_TOOLS_PREAMBLE + _BASE_COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions.strip()}"
    return prompt


async def deny_compact_tools(_tool_use: ToolUseBlock) -> CanUseDecision:
    return CanUseDecision(
        allow=False,
        reason="full compact fork only produces a text summary",
    )


def format_compact_summary(raw_summary: str) -> str:
    summary = re.sub(r"<analysis>[\s\S]*?</analysis>", "", raw_summary, count=1)
    match = re.search(r"<summary>([\s\S]*?)</summary>", summary)
    if match:
        summary = f"Summary:\n{match.group(1).strip()}"
    summary = re.sub(r"\n\n+", "\n\n", summary)
    return summary.strip()


def build_full_compact_summary_message(
    raw_summary: str,
    transcript_path: str | None = None,
) -> UserMessage:
    content = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier conversation.\n\n"
        f"{format_compact_summary(raw_summary)}"
    )
    if transcript_path:
        content += (
            "\n\nIf exact details from before compaction are needed, inspect the transcript at: "
            f"{transcript_path}"
        )
    content += (
        "\n\nContinue from where the conversation left off without asking follow-up questions. "
        "Do not acknowledge or recap this summary; resume the latest task directly."
    )
    return UserMessage(content=content)


def _assistant_text(messages: list[Message]) -> str | None:
    for message in reversed(messages):
        if not isinstance(message, AssistantMessage):
            continue
        text = "".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        ).strip()
        if text:
            return text
    return None


def _is_tool_result_message(message: Message) -> bool:
    return (
        isinstance(message, UserMessage)
        and isinstance(message.content, list)
        and bool(message.content)
        and all(isinstance(block, ToolResultBlock) for block in message.content)
    )


def _group_api_rounds(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    for message in messages:
        starts_round = isinstance(message, UserMessage) and not _is_tool_result_message(message)
        if starts_round or not groups:
            groups.append([message])
        else:
            groups[-1].append(message)
    return groups


def truncate_head_for_compact_retry(messages: list[Message]) -> list[Message] | None:
    """compact 请求也超限时,丢掉最旧约 20% API rounds,最多由调用方重试三次。"""
    groups = _group_api_rounds(
        [
            message
            for message in messages
            if not isinstance(message, CompactBoundaryMessage)
        ]
    )
    if len(groups) < 2:
        return None
    drop_count = min(max(1, len(groups) // 5), len(groups) - 1)
    kept = [message for group in groups[drop_count:] for message in group]
    if kept and isinstance(kept[0], AssistantMessage):
        kept.insert(0, UserMessage(content=_PTL_RETRY_MARKER))
    return kept


async def _stream_summary_fallback(
    messages: list[Message],
    prompt: str,
    params: "QueryParams",
    tracer: "Tracer",
) -> str | None:
    """fork 无文本/异常时的独立 summarizer；不执行工具，也不追求父缓存命中。"""
    from .loop.phases.stream_turn import aggregate_stream

    events = params.provider.stream(
        messages=[*messages, UserMessage(content=prompt)],
        system="You are a helpful AI assistant tasked with summarizing conversations.",
        tools=[],
        model=params.model,
        max_tokens=min(COMPACT_MAX_OUTPUT_TOKENS, params.max_tokens),
        abort_signal=params.abort_signal,
        tracer=tracer,
    )
    blocks: list[TextBlock] = []
    async for item in aggregate_stream(events, tracer):
        if isinstance(item, AssistantMessage):
            blocks.extend(
                block for block in item.content if isinstance(block, TextBlock)
            )
    text = "".join(block.text for block in blocks).strip()
    return text or None


async def _generate_summary(
    agent_state: AgentState,
    params: "QueryParams",
    tracer: "Tracer",
    custom_instructions: str | None,
) -> str | None:
    prompt = build_full_compact_prompt(custom_instructions)
    messages_to_summarize = list(agent_state.messages)
    ptl_attempts = 0

    while True:
        fork_failed = False
        try:
            fork_state = await run_forked_agent(
                parent_agent_state=agent_state,
                parent_params=params,
                task_prompt=prompt,
                tracer=tracer,
                can_use_tool=deny_compact_tools,
                max_turns=1,
                fork_context_messages=messages_to_summarize,
                abort_signal=params.abort_signal,
                propagate_errors=True,
            )
            summary = _assistant_text(fork_state.messages[len(messages_to_summarize) + 1 :])
            if summary:
                return summary
            fork_failed = True
        except ForkedAgentError as error:
            if error.terminal.reason is TerminalReason.PROMPT_TOO_LONG:
                ptl_attempts += 1
                truncated = (
                    truncate_head_for_compact_retry(messages_to_summarize)
                    if ptl_attempts <= MAX_PROMPT_TOO_LONG_RETRIES
                    else None
                )
                if truncated is None:
                    return None
                messages_to_summarize = truncated
                continue
            fork_failed = True
        except Exception as error:  # noqa: BLE001
            logger.warning("full compact cache-sharing fork failed: %s", error)
            fork_failed = True

        if fork_failed:
            try:
                summary = await _stream_summary_fallback(
                    messages_to_summarize, prompt, params, tracer
                )
                if summary:
                    return summary
                return None
            except PromptTooLongError:
                ptl_attempts += 1
                truncated = (
                    truncate_head_for_compact_retry(messages_to_summarize)
                    if ptl_attempts <= MAX_PROMPT_TOO_LONG_RETRIES
                    else None
                )
                if truncated is None:
                    return None
                messages_to_summarize = truncated
            except Exception as error:  # noqa: BLE001
                logger.warning("full compact streaming fallback failed: %s", error)
                return None


async def _restore_recent_files(agent_state: AgentState) -> UserMessage | None:
    """清旧 Read 锁并重新读取最近文件,让压缩后的模型与乐观锁看到同一份新快照。"""
    snapshot = agent_state.file_read_state.items()
    agent_state.file_read_state.clear()
    if not snapshot:
        return None

    from .session_memory import get_session_memory_path

    notes_path = get_session_memory_path(agent_state.cwd)
    snippets: list[str] = []
    used_chars = 0
    per_file_chars = POST_COMPACT_MAX_TOKENS_PER_FILE * 4
    budget_chars = POST_COMPACT_FILES_TOKEN_BUDGET * 4

    for path, old_state in reversed(snapshot[-POST_COMPACT_MAX_FILES:]):
        if path == notes_path:
            continue
        try:
            offset = old_state.offset if old_state.offset is not None else None
            numbered, fresh_state, absolute_path = await read(
                path, offset=offset, limit=old_state.limit
            )
        except (OSError, ValueError):
            continue
        content = numbered[:per_file_chars]
        if used_chars + len(content) > budget_chars:
            continue
        used_chars += len(content)
        agent_state.file_read_state.set(absolute_path, fresh_state)
        snippets.append(f"<file path=\"{absolute_path}\">\n{content}\n</file>")

    if not snippets:
        return None
    return UserMessage(
        content=(
            "<system-reminder>\n"
            "Recently read files were refreshed after context compaction:\n\n"
            + "\n\n".join(snippets)
            + "\n</system-reminder>"
        )
    )


async def _archive_precompact_transcript(
    messages: list[Message],
    transcript_path: str | None,
) -> str | None:
    """LE 主 transcript 会覆写,故另存本次压缩前快照供摘要中的路径长期可用。"""
    if not transcript_path or not messages:
        return None
    from .transcript import record_transcript

    source = Path(transcript_path)
    last_uuid = getattr(messages[-1], "uuid", "unknown")
    archive = source.with_name(f"{source.name}.precompact.{last_uuid}.jsonl")
    try:
        await record_transcript(messages, archive)
    except OSError as error:
        logger.warning("failed to archive pre-compact transcript: %s", error)
        return None
    return str(archive)


async def full_compact(
    agent_state: AgentState,
    params: "QueryParams",
    tracer: "Tracer",
    *,
    trigger: Literal["auto", "manual"] = "auto",
    pre_compact_tokens: int | None = None,
    custom_instructions: str | None = None,
) -> bool:
    """生成全量摘要并原地替换 messages；失败时保持父状态不变。"""
    if not agent_state.messages:
        return False

    original_messages = list(agent_state.messages)
    tracer.emit(
        TraceEvent(
            kind=TraceKind.COMPACT_START,
            payload={"strategy": "full", "trigger": trigger},
        )
    )
    summary = await _generate_summary(
        agent_state, params, tracer, custom_instructions
    )
    if not summary:
        tracer.emit(
            TraceEvent(
                kind=TraceKind.COMPACT_END,
                payload={
                    "strategy": "full",
                    "trigger": trigger,
                    "success": False,
                    "reason": "no_summary",
                },
            )
        )
        return False

    if pre_compact_tokens is not None:
        token_count = pre_compact_tokens
    else:
        try:
            token_count = params.provider.count_tokens(original_messages)
        except Exception:  # noqa: BLE001
            token_count = 0
    boundary = CompactBoundaryMessage(
        trigger=trigger,
        pre_tokens=token_count,
        last_pre_compact_message_uuid=getattr(original_messages[-1], "uuid", None),
    )
    transcript_archive = await _archive_precompact_transcript(
        original_messages, getattr(params, "transcript_path", None)
    )
    summary_message = build_full_compact_summary_message(summary, transcript_archive)

    file_attachment = await _restore_recent_files(agent_state)
    post_compact: list[Message] = [boundary, summary_message]
    if file_attachment is not None:
        post_compact.append(file_attachment)
    agent_state.messages[:] = post_compact

    # Full compact 删除了旧 skill listing；显式重建,否则 sent_skill_names 会阻止再次注入。
    from .loop.phases.skill_listing import inject_skill_listing

    agent_state.sent_skill_names.clear()
    inject_skill_listing(agent_state)
    agent_state.sm.generation += 1
    agent_state.sm.last_summarized_message_uuid = None
    try:
        post_compact_tokens = params.provider.count_tokens(agent_state.messages)
    except Exception:  # noqa: BLE001
        post_compact_tokens = 0
    tracer.emit(
        TraceEvent(
            kind=TraceKind.COMPACT_END,
            payload={
                "strategy": "full",
                "trigger": trigger,
                "success": True,
                "pre_tokens": token_count,
                "post_tokens": post_compact_tokens,
            },
        )
    )
    return True
