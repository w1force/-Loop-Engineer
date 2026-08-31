"""Verification 子 Agent 的上下文隔离与普通 tool_result 语义。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.builtin_tools import (
    AGENT_TOOL,
    BASH_TOOL,
    GLOB_TOOL,
    GREP_TOOL,
    LOAD_SKILL_TOOL,
    READ_TOOL,
)
from core.forked_agent import run_subagent
from core.loop.orchestrator import QueryParams
from core.tool_executor import make_executor
from core.tools import ToolContext, default_can_use_tool
from core.types import (
    AgentState,
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    SkillMeta,
)
from telemetry.file_tracer import FileTracer
from telemetry.tracer import NoopTracer


def _text_events(text: str):
    async def events():
        for event in [
            StreamEvent(type="message_start"),
            StreamEvent(
                type="content_block_start",
                index=0,
                block={"type": "text", "text": ""},
            ),
            StreamEvent(
                type="content_block_delta",
                index=0,
                delta={"text": text},
            ),
            StreamEvent(type="content_block_stop", index=0),
            StreamEvent(
                type="message_delta",
                delta={"stop_reason": "end_turn"},
                message={"usage": {"input_tokens": 3, "output_tokens": 2}},
            ),
            StreamEvent(type="message_stop"),
        ]:
            yield event

    return events()


def _thinking_text_events(reasoning: str, text: str):
    async def events():
        for event in [
            StreamEvent(type="message_start"),
            StreamEvent(
                type="content_block_start",
                index=0,
                block={"type": "thinking", "thinking": "", "signature": ""},
            ),
            StreamEvent(
                type="content_block_delta",
                index=0,
                delta={"thinking": reasoning},
            ),
            StreamEvent(type="content_block_stop", index=0),
            StreamEvent(
                type="content_block_start",
                index=1,
                block={"type": "text", "text": ""},
            ),
            StreamEvent(
                type="content_block_delta",
                index=1,
                delta={"text": text},
            ),
            StreamEvent(type="content_block_stop", index=1),
            StreamEvent(
                type="message_delta",
                delta={"stop_reason": "end_turn"},
                message={"usage": {"input_tokens": 3, "output_tokens": 2}},
            ),
            StreamEvent(type="message_stop"),
        ]:
            yield event

    return events()


class RecordingProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "messages": [
                    message.model_copy(deep=True)
                    for message in kwargs["messages"]
                ],
                "tools": list(kwargs["tools"]),
            }
        )
        return self.responses[len(self.calls) - 1]

    def count_tokens(self, messages):
        return 0


def _params(provider, transcript_path=None):
    return QueryParams(
        system="main system",
        model="test-model",
        max_tokens=128,
        provider=provider,
        abort_signal=asyncio.Event(),
        tools=[
            READ_TOOL,
            GLOB_TOOL,
            GREP_TOOL,
            BASH_TOOL,
            AGENT_TOOL,
        ],
        transcript_path=transcript_path,
        verification_agent_max_turns=7,
    )


async def test_fresh_subagent_does_not_inherit_parent_claims(tmp_path):
    provider = RecordingProvider([_text_events("independent\nVERDICT: PASS")])
    parent = AgentState(
        cwd=str(tmp_path),
        messages=[
            UserMessage(content="fix the timeout"),
            AssistantMessage(content=[TextBlock(text="all tests already pass")]),
        ],
    )
    original_messages = list(parent.messages)

    result = await run_subagent(
        parent_agent_state=parent,
        parent_params=_params(provider),
        task_prompt="verify the timeout fix",
        tracer=NoopTracer(),
        context_mode="fresh",
        system_override="verification system",
        tools_override=[READ_TOOL],
        cwd_override=str(tmp_path),
        abort_signal=asyncio.Event(),
        propagate_errors=True,
    )

    request = provider.calls[0]
    assert request["model"] == "test-model"
    assert request["system"] == "verification system"
    assert [tool.name for tool in request["tools"]] == ["Read"]
    assert len(request["messages"]) == 1
    assert request["messages"][0].content == "verify the timeout fix"
    assert "all tests already pass" not in str(request["messages"])
    assert parent.messages == original_messages
    assert result.final_text.endswith("VERDICT: PASS")
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 2


async def test_fresh_subagent_receives_only_explicit_skill_snapshot_and_records_trajectory(
    tmp_path,
):
    provider = RecordingProvider([_text_events("done")])
    skill_path = tmp_path / "skills" / "learned-repair-timeout" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("live content", encoding="utf-8")
    skill = SkillMeta(
        name="learned-repair-timeout",
        description="timeout history",
        skill_dir=skill_path.parent,
        skill_md=skill_path,
        snapshot_text="frozen content",
        digest="a" * 64,
    )
    parent = AgentState(cwd=str(tmp_path))
    params = _params(provider, transcript_path=None)
    params.tools.append(LOAD_SKILL_TOOL)

    result = await run_subagent(
        parent_agent_state=parent,
        parent_params=params,
        task_prompt="repair incident",
        tracer=FileTracer(str(tmp_path / "trace.jsonl")),
        tools_override=[READ_TOOL, LOAD_SKILL_TOOL],
        skills_override=[skill],
        trajectory_context={
            "run_id": "run-1",
            "incident_id": "incident-1",
            "stage": "repair",
            "cycle": 1,
        },
        propagate_errors=True,
    )

    messages = provider.calls[0]["messages"]
    assert len(messages) == 2
    assert "learned-repair-timeout" in str(messages[1].content)
    assert result.agent_state.skills == [skill]
    assert result.trajectory_path is not None
    assert (tmp_path / Path(result.trajectory_path).name).is_file()


async def test_repair_trajectory_separates_reasoning_from_final_handoff(tmp_path):
    handoff = '{"implementation_summary":"done","test_entrypoints":["pytest -q"]}'
    provider = RecordingProvider([_thinking_text_events("inspect root cause", handoff)])
    context = {
        "run_id": "run-thinking",
        "incident_id": "incident-thinking",
        "stage": "repair",
        "cycle": 1,
    }
    trace_path = tmp_path / "trace.jsonl"

    result = await run_subagent(
        parent_agent_state=AgentState(cwd=str(tmp_path)),
        parent_params=_params(provider),
        task_prompt="repair incident",
        tracer=FileTracer(str(trace_path), ctx=context),
        tools_override=[READ_TOOL],
        transcript_path=str(tmp_path / "repair.transcript.jsonl"),
        trajectory_context=context,
        propagate_errors=True,
    )

    assert result.final_text == handoff
    assistant = next(
        message
        for message in result.agent_state.messages
        if isinstance(message, AssistantMessage)
    )
    assert isinstance(assistant.content[0], ThinkingBlock)
    trajectory = json.loads(Path(result.trajectory_path or "").read_text(encoding="utf-8"))
    assert trajectory["reasoning_blocks"][0]["thinking"] == "inspect root cause"
    assert trajectory["conversations"][-1]["value"] == handoff
    assert trajectory["conversations"][-1]["reasoning"][0]["thinking"] == (
        "inspect root cause"
    )


async def test_agent_tool_returns_fail_as_text_not_hard_gate(tmp_path):
    report = "reproduced regression\nVERDICT: FAIL"
    provider = RecordingProvider([_text_events(report)])
    parent = AgentState(cwd=str(tmp_path))
    params = _params(provider, str(tmp_path / "main.transcript.jsonl"))
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=params.abort_signal,
        agent_state=parent,
        query_params=params,
    )
    executor = make_executor(
        "batch",
        params.tools,
        default_can_use_tool,
        NoopTracer(),
        ctx,
    )
    executor.add_tool(
        ToolUseBlock(
            id="agent-1",
            name="Agent",
            input={
                "description": "verify timeout fix",
                "prompt": "original task; changed: service.py; approach: bounded timeout",
                "subagent_type": "verification",
            },
        )
    )

    result = (await executor.get_results())[0]

    assert result.is_error is False
    assert result.content == report
    assert parent.total_input_tokens == 3
    assert parent.total_output_tokens == 2
    assert list(tmp_path.glob("main.transcript.verification-*.jsonl"))


async def test_builtin_verification_agent_cannot_read_learned_repair_history(
    tmp_path, monkeypatch
):
    custom_history = tmp_path.parent / "operator" / "learned"
    custom_archive = tmp_path.parent / "operator" / "archive"
    custom_history.mkdir(parents=True, exist_ok=True)
    custom_archive.mkdir(parents=True, exist_ok=True)
    (custom_history / "SKILL.md").write_text("custom historical fix", encoding="utf-8")
    monkeypatch.setenv("LOOP_ENGINEER_LEARNED_SKILLS_ROOT", str(custom_history))
    monkeypatch.setenv("LOOP_ENGINEER_LEARNING_ARCHIVE_ROOT", str(custom_archive))
    history = tmp_path / ".loop-engineer" / "learned-repair-skills"
    history.mkdir(parents=True)
    (history / "SKILL.md").write_text("historical fix", encoding="utf-8")
    safe_file = tmp_path / "service.py"
    safe_file.write_text("status = 'ok'\n", encoding="utf-8")
    decisions = {}

    async def fake_run_subagent(**kwargs):
        guard = kwargs["can_use_tool"]
        decisions["history"] = await guard(
            ToolUseBlock(
                id="history",
                name="Read",
                input={
                    "file_path": ".loop-engineer/learned-repair-skills/SKILL.md"
                },
            )
        )
        decisions["root_search"] = await guard(
            ToolUseBlock(
                id="root-search",
                name="Grep",
                input={"pattern": "historical"},
            )
        )
        decisions["source"] = await guard(
            ToolUseBlock(
                id="source",
                name="Read",
                input={"file_path": "service.py"},
            )
        )
        return SimpleNamespace(
            successful=True,
            error=None,
            terminal=SimpleNamespace(error=None),
            final_text="focused evidence\nVERDICT: PASS",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )

    monkeypatch.setattr(
        "core.builtin_tools.agent.run_subagent", fake_run_subagent
    )
    parent = AgentState(cwd=str(tmp_path))
    params = _params(RecordingProvider([]))
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=params.abort_signal,
        agent_state=parent,
        query_params=params,
    )
    inp = AGENT_TOOL.input_model(
        description="verify repair",
        prompt="Inspect the candidate independently.",
        subagent_type="verification",
    )

    report = await AGENT_TOOL.func(inp, ctx)

    assert report.endswith("VERDICT: PASS")
    assert decisions["history"].allow is False
    assert decisions["root_search"].allow is False
    assert decisions["source"].allow is True


async def test_verification_fails_closed_when_custom_history_overlaps_workspace(
    tmp_path, monkeypatch
):
    custom_history = tmp_path / "operator" / "learned"
    custom_history.mkdir(parents=True)
    monkeypatch.setenv("LOOP_ENGINEER_LEARNED_SKILLS_ROOT", str(custom_history))
    monkeypatch.setenv(
        "LOOP_ENGINEER_LEARNING_ARCHIVE_ROOT", str(tmp_path.parent / "archive")
    )
    monkeypatch.setattr(
        "core.builtin_tools.agent.run_subagent",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not start")),
    )
    parent = AgentState(cwd=str(tmp_path))
    params = _params(RecordingProvider([]))
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=params.abort_signal,
        agent_state=parent,
        query_params=params,
    )
    inp = AGENT_TOOL.input_model(
        description="verify repair",
        prompt="Inspect the candidate independently.",
        subagent_type="verification",
    )

    with pytest.raises(ValueError, match="protected control-plane root"):
        await AGENT_TOOL.func(inp, ctx)
