"""ShareGPT export and head/summary/tail trajectory compression."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Protocol

from core.tools import Tool
from core.types import (
    AssistantMessage,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .models import (
    CompressedRepairTrajectory,
    HumanReviewDecision,
    ShareGPTTrajectory,
)


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[-_]?key|cookie|password|secret|token)", re.IGNORECASE
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?<![A-Z0-9_.-])["']?[A-Z0-9_.-]{0,64}
        (?:AUTHORIZATION|API[-_]?KEY|COOKIE|PASSWORD|SECRET|TOKEN)
        [A-Z0-9_.-]{0,64}["']?[ \t]*[:=][ \t]*
    )
    (?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]]+)
    """
)
_OPENAI_TOKEN = re.compile(r"(?i)\bsk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{6,}\b")
_GITHUB_TOKEN = re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{8,}\b")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)


class TrajectoryCompressionError(ValueError):
    """The mandatory protected trajectory regions cannot fit the target budget."""


class TextGenerator(Protocol):
    async def generate(
        self, *, system: str, prompt: str, model: str, max_tokens: int
    ) -> str: ...


@dataclass(frozen=True)
class CompressionConfig:
    # Metadata must describe the counter actually used. Callers that inject an
    # exact Kimi counter should set this to that tokenizer's model identifier.
    tokenizer_name: str = "approximate:utf8-bytes-div-3"
    target_max_tokens: int = 15_250
    summary_target_tokens: int = 750
    protect_last_n_turns: int = 4
    summarization_model: str = "google/gemini-3-flash"

    def __post_init__(self) -> None:
        if not self.tokenizer_name.strip():
            raise ValueError("tokenizer_name must be non-empty")
        if self.target_max_tokens < 1:
            raise ValueError("target_max_tokens must be positive")
        if self.summary_target_tokens < 1:
            raise ValueError("summary_target_tokens must be positive")
        if self.protect_last_n_turns < 1:
            raise ValueError("protect_last_n_turns must be positive")


def approximate_token_count(text: str) -> int:
    """Conservative fallback; production may inject the configured tokenizer."""

    return max(1, math.ceil(len(text.encode("utf-8")) / 3))


def _redact_string(value: str) -> str:
    value = _PEM_PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)

    def redact_assignment(match: re.Match[str]) -> str:
        raw = match.group("value")
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            replacement = raw[0] + "[REDACTED]" + raw[-1]
        else:
            replacement = "[REDACTED]"
        return match.group("prefix") + replacement

    value = _SENSITIVE_ASSIGNMENT.sub(redact_assignment, value)
    value = _OPENAI_TOKEN.sub("[REDACTED OPENAI TOKEN]", value)
    value = _GITHUB_TOKEN.sub("[REDACTED GITHUB TOKEN]", value)
    return _AWS_ACCESS_KEY.sub("[REDACTED AWS ACCESS KEY]", value)


def _redact(value: Any, *, key: str = "") -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_sensitive(value: Any) -> Any:
    """Return a recursively redacted copy suitable for a durable trajectory."""

    return _redact(value)


def _system_text(system: str | list[dict]) -> str:
    if isinstance(system, str):
        return system
    parts: list[str] = []
    for block in system:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(json.dumps(block, ensure_ascii=False, default=str))
    return "\n".join(parts)


def messages_to_sharegpt(
    *, system: str | list[dict], messages: list[Message]
) -> tuple[dict[str, Any], ...]:
    conversations: list[dict[str, Any]] = [
        {"from": "system", "value": _system_text(system)}
    ]
    for message in messages:
        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                conversations.append({"from": "human", "value": message.content})
                continue
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    conversations.append(
                        {
                            "from": "tool",
                            "value": block.content,
                            "tool_use_id": block.tool_use_id,
                            "is_error": block.is_error,
                        }
                    )
                else:
                    conversations.append(
                        {"from": "human", "value": block.model_dump(mode="json")}
                    )
            continue
        if isinstance(message, AssistantMessage):
            texts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            reasoning: list[dict[str, Any]] = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append(block.model_dump(mode="json"))
                elif isinstance(block, ThinkingBlock):
                    reasoning.append(
                        {"type": "thinking", "thinking": block.thinking}
                    )
                elif isinstance(block, RedactedThinkingBlock):
                    reasoning.append(
                        {"type": "redacted_thinking", "thinking": None}
                    )
            item: dict[str, Any] = {"from": "gpt", "value": "".join(texts)}
            if tool_calls:
                item["tool_calls"] = tool_calls
            if reasoning:
                item["reasoning"] = reasoning
            conversations.append(item)
    return tuple(_redact(conversations))


def _trace_context(tracer: Any) -> tuple[str | None, dict[str, Any]]:
    path = getattr(tracer, "path", None)
    context = getattr(tracer, "context", {})
    return (str(path) if path else None, dict(context) if isinstance(context, dict) else {})


def extract_reasoning_blocks(
    trace_path: str | None, *, context: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Read only provider-visible thinking blocks; hidden reasoning is unavailable."""

    if not trace_path:
        return ()
    source = Path(trace_path)
    if not source.is_file():
        return ()
    selected: list[dict[str, Any]] = []
    try:
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if record.get("kind") != "llm_response":
                    continue
                if any(record.get(key) != value for key, value in context.items()):
                    continue
                for block in record.get("payload", {}).get("blocks", []):
                    if isinstance(block, dict) and block.get("type") in {
                        "thinking",
                        "redacted_thinking",
                    }:
                        selected.append(
                            {
                                "turn": record.get("turn"),
                                "type": block.get("type"),
                                "thinking": block.get("thinking"),
                            }
                        )
    except OSError:
        return ()
    return tuple(_redact(selected))


def reasoning_blocks_from_messages(
    messages: list[Message],
) -> tuple[dict[str, Any], ...]:
    """Extract provider-visible reasoning without depending on telemetry storage."""

    selected: list[dict[str, Any]] = []
    turn = 0
    for message in messages:
        if not isinstance(message, AssistantMessage):
            continue
        turn += 1
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                selected.append(
                    {"turn": turn, "type": "thinking", "thinking": block.thinking}
                )
            elif isinstance(block, RedactedThinkingBlock):
                selected.append(
                    {"turn": turn, "type": "redacted_thinking", "thinking": None}
                )
    return tuple(_redact(selected))


def _merge_reasoning_blocks(
    primary: tuple[dict[str, Any], ...],
    supplemental: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    merged = list(primary)
    seen = {
        (
            item.get("turn"),
            item.get("type"),
            json.dumps(item.get("thinking"), sort_keys=True, default=str),
        )
        for item in primary
    }
    for item in supplemental:
        identity = (
            item.get("turn"),
            item.get("type"),
            json.dumps(item.get("thinking"), sort_keys=True, default=str),
        )
        if identity not in seen:
            merged.append(item)
            seen.add(identity)
    return tuple(merged)


def sharegpt_path_for(transcript_path: str | Path) -> Path:
    source = Path(transcript_path)
    return source.with_name(source.name + ".sharegpt.jsonl")


def _write_json_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".trajectory-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def record_sharegpt_trajectory(
    *,
    transcript_path: str | Path,
    system: str | list[dict],
    messages: list[Message],
    tools: list[Tool],
    model: str,
    completed: bool,
    terminal_reason: str,
    context: dict[str, Any],
    tracer: Any,
) -> str:
    trace_path, trace_context = _trace_context(tracer)
    merged_context = {**trace_context, **context}
    record = ShareGPTTrajectory(
        run_id=str(context["run_id"]),
        incident_id=str(context["incident_id"]),
        cycle=int(context["cycle"]),
        conversations=messages_to_sharegpt(system=system, messages=messages),
        model=model,
        completed=completed,
        terminal_reason=terminal_reason,
        trace_path=trace_path,
        tools=tuple(_redact(tool.to_schema()) for tool in tools),
        reasoning_blocks=_merge_reasoning_blocks(
            reasoning_blocks_from_messages(messages),
            extract_reasoning_blocks(
                trace_path,
                context={
                    key: merged_context[key]
                    for key in ("run_id", "incident_id", "stage", "cycle")
                    if key in merged_context
                },
            ),
        ),
    )
    payload = record.model_dump(mode="json")
    target = sharegpt_path_for(transcript_path)
    await asyncio.to_thread(_write_json_line, target, _redact(payload))
    return str(target)


def load_sharegpt_trajectory(path: str | Path) -> ShareGPTTrajectory:
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                payload = json.loads(line)
                return ShareGPTTrajectory.model_validate(payload)
    raise ValueError("ShareGPT trajectory is empty")


def _serialized_tokens(
    conversations: tuple[dict[str, Any], ...],
    counter: Callable[[str], int],
    reasoning_blocks: tuple[dict[str, Any], ...] = (),
) -> int:
    return counter(
        json.dumps(
            {
                "conversations": conversations,
                "reasoning_blocks": reasoning_blocks,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _usable_reasoning_count(trajectory: ShareGPTTrajectory) -> int:
    return sum(
        1
        for block in trajectory.reasoning_blocks
        if block.get("type") == "thinking"
        and isinstance(block.get("thinking"), str)
        and bool(block["thinking"].strip())
    )


def loaded_skill_names(
    conversations: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    """Freeze every learned Skill actually loaded before middle compression."""

    names: list[str] = []
    for item in conversations:
        calls = item.get("tool_calls", ())
        if not isinstance(calls, (list, tuple)):
            continue
        for call in calls:
            if not isinstance(call, dict) or call.get("name") != "Load_Skill":
                continue
            arguments = call.get("input")
            name = arguments.get("name") if isinstance(arguments, dict) else None
            if isinstance(name, str) and name not in names:
                names.append(name)
    return tuple(names)


def _protected_ranges(
    conversations: tuple[dict[str, Any], ...], last_n_turns: int
) -> tuple[int, int]:
    """Return head end and tail start while preserving complete assistant rounds."""

    head_end = 0
    while head_end < len(conversations) and conversations[head_end].get("from") == "system":
        head_end += 1
    if head_end < len(conversations):
        # Skill listing may be injected immediately before the actual task prompt.
        # Preserve through the first Repair prompt, which embeds frozen Diagnosis.
        repair_prompt = next(
            (
                index
                for index in range(head_end, len(conversations))
                if conversations[index].get("from") in {"human", "user"}
                and "INCIDENT_JSON:" in str(conversations[index].get("value") or "")
            ),
            None,
        )
        head_end = (repair_prompt + 1) if repair_prompt is not None else (head_end + 1)

    assistant = [
        index
        for index, item in enumerate(conversations)
        if item.get("from") in {"assistant", "gpt"}
    ]
    if len(assistant) <= last_n_turns:
        return head_end, head_end
    return head_end, max(head_end, assistant[-last_n_turns])


_SUMMARY_SYSTEM = (
    "You compress the middle of a repair-agent trajectory. Return factual text only. "
    "Preserve attempted fixes, tool evidence, user corrections, failures and why each "
    "attempt changed. Do not claim success and do not invent missing evidence."
)


class TrajectoryCompressor:
    def __init__(
        self,
        *,
        generator: TextGenerator,
        config: CompressionConfig | None = None,
        token_counter: Callable[[str], int] = approximate_token_count,
    ) -> None:
        self.generator = generator
        self.config = config or CompressionConfig()
        self.token_counter = token_counter

    async def compress(
        self,
        trajectory: ShareGPTTrajectory,
        *,
        review: HumanReviewDecision,
    ) -> CompressedRepairTrajectory:
        # Re-scan even persisted trajectories before sending their middle section to
        # an external summarizer. This also protects callers that constructed a
        # ShareGPTTrajectory without going through record_sharegpt_trajectory().
        conversations = tuple(_redact(trajectory.conversations))
        reasoning_blocks = tuple(_redact(trajectory.reasoning_blocks))
        loaded_names = loaded_skill_names(conversations)
        original_tokens = _serialized_tokens(
            conversations,
            self.token_counter,
            reasoning_blocks,
        )
        if original_tokens <= self.config.target_max_tokens:
            return CompressedRepairTrajectory(
                run_id=trajectory.run_id,
                incident_id=trajectory.incident_id,
                cycle=trajectory.cycle,
                review=review,
                source_trajectory_digest=trajectory.digest,
                conversations=conversations,
                loaded_skill_names=loaded_names,
                reasoning_blocks=reasoning_blocks,
                reasoning_block_count=len(trajectory.reasoning_blocks),
                usable_reasoning_count=_usable_reasoning_count(trajectory),
                source_completed=trajectory.completed,
                terminal_reason=trajectory.terminal_reason,
                compressed=False,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                tokenizer_name=self.config.tokenizer_name,
                summarization_model=self.config.summarization_model,
            )

        head_end, tail_start = _protected_ranges(
            conversations, self.config.protect_last_n_turns
        )
        head = conversations[:head_end]
        middle = conversations[head_end:tail_start]
        tail = conversations[tail_start:]
        if not middle:
            raise TrajectoryCompressionError(
                "trajectory exceeds target_max_tokens but has no compressible middle: "
                f"target={self.config.target_max_tokens}, protected={original_tokens}"
            )
        else:
            minimum = (
                *head,
                {"from": "system", "value": "[COMPRESSED_TRAJECTORY_MIDDLE]\n"},
                *tail,
            )
            minimum_tokens = _serialized_tokens(tuple(minimum), self.token_counter)
            if minimum_tokens > self.config.target_max_tokens:
                raise TrajectoryCompressionError(
                    "protected trajectory head/tail exceed target_max_tokens: "
                    f"target={self.config.target_max_tokens}, "
                    f"minimum={minimum_tokens}"
                )
            summary = (
                await self.generator.generate(
                    system=_SUMMARY_SYSTEM,
                    prompt=(
                        "Summarize only TRAJECTORY_MIDDLE_JSON below. The immutable "
                        "head and final four turns are retained separately.\n\n"
                        + json.dumps(
                            {
                                "conversations": middle,
                                "reasoning_blocks": reasoning_blocks,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    ),
                    model=self.config.summarization_model,
                    max_tokens=self.config.summary_target_tokens,
                )
            ).strip()
            if not summary:
                raise ValueError("trajectory summarizer returned empty text")
            summary = _redact_string(summary)
            compressed = (
                *head,
                {
                    "from": "system",
                    "value": "[COMPRESSED_TRAJECTORY_MIDDLE]\n" + summary,
                },
                *tail,
            )
        compressed_tokens = _serialized_tokens(tuple(compressed), self.token_counter)
        if compressed_tokens > self.config.target_max_tokens:
            raise TrajectoryCompressionError(
                "trajectory summarization did not meet target_max_tokens: "
                f"target={self.config.target_max_tokens}, "
                f"compressed={compressed_tokens}"
            )
        return CompressedRepairTrajectory(
            run_id=trajectory.run_id,
            incident_id=trajectory.incident_id,
            cycle=trajectory.cycle,
            review=review,
            source_trajectory_digest=trajectory.digest,
            conversations=tuple(compressed),
            loaded_skill_names=loaded_names,
            reasoning_blocks=(),
            reasoning_block_count=len(trajectory.reasoning_blocks),
            usable_reasoning_count=_usable_reasoning_count(trajectory),
            source_completed=trajectory.completed,
            terminal_reason=trajectory.terminal_reason,
            compressed=True,
            summary=summary,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            tokenizer_name=self.config.tokenizer_name,
            summarization_model=self.config.summarization_model,
        )


__all__ = [
    "CompressionConfig",
    "TextGenerator",
    "TrajectoryCompressionError",
    "TrajectoryCompressor",
    "approximate_token_count",
    "extract_reasoning_blocks",
    "load_sharegpt_trajectory",
    "loaded_skill_names",
    "messages_to_sharegpt",
    "reasoning_blocks_from_messages",
    "redact_sensitive",
    "record_sharegpt_trajectory",
    "sharegpt_path_for",
]
