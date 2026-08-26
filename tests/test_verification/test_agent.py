"""Verification 子 Agent 的上下文隔离与普通 tool_result 语义。"""

import asyncio

from core.builtin_tools import AGENT_TOOL, BASH_TOOL, GLOB_TOOL, GREP_TOOL, READ_TOOL
from core.forked_agent import run_subagent
from core.loop.orchestrator import QueryParams
from core.tool_executor import make_executor
from core.tools import ToolContext, default_can_use_tool
from core.types import (
    AgentState,
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
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
