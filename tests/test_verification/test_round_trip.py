"""主 Agent -> verifier -> Bash -> verifier report -> 主 Agent 的完整同步链。"""

import asyncio
import json

from pydantic import BaseModel

from core.builtin_tools import AGENT_TOOL
from core.loop.orchestrator import QueryParams, query_loop
from core.tools import Tool, build_tool, default_can_use_tool
from core.types import AgentState, StreamEvent, ToolResultBlock, UserMessage
from telemetry.tracer import NoopTracer


class EmptyInput(BaseModel):
    pass


class BashInput(BaseModel):
    command: str
    timeout: int | None = None
    description: str | None = None


async def _noop(_inp, _ctx):
    return "unused"


async def _bash(inp: BashInput, _ctx):
    return f"executed={inp.command}\n1 passed in 0.03s"


def _tool(name: str, input_model=EmptyInput, func=_noop):
    return build_tool(
        name=name,
        description=f"mock {name}",
        input_model=input_model,
        func=func,
        is_concurrency_safe=name != "Bash",
    )


def _tool_events(name: str, payload: dict, tool_id: str):
    async def events():
        for event in [
            StreamEvent(type="message_start"),
            StreamEvent(
                type="content_block_start",
                index=0,
                block={"type": "tool_use", "id": tool_id, "name": name},
            ),
            StreamEvent(
                type="content_block_delta",
                index=0,
                delta={"tool_input": json.dumps(payload)},
            ),
            StreamEvent(type="content_block_stop", index=0),
            StreamEvent(
                type="message_delta",
                delta={"stop_reason": "tool_use"},
                message={"usage": {"input_tokens": 5, "output_tokens": 3}},
            ),
            StreamEvent(type="message_stop"),
        ]:
            yield event

    return events()


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
                message={"usage": {"input_tokens": 7, "output_tokens": 4}},
            ),
            StreamEvent(type="message_stop"),
        ]:
            yield event

    return events()


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "messages": [m.model_copy(deep=True) for m in kwargs["messages"]],
                "tools": list(kwargs["tools"]),
            }
        )
        return self.responses[len(self.calls) - 1]

    def count_tokens(self, messages):
        return 0


async def test_verification_agent_executes_dynamic_command_and_returns_report(tmp_path):
    agent_prompt = (
        "Original task: fix timeout. Changed files: service.py, test_timeout.py. "
        "Approach: add a bounded timeout."
    )
    report = (
        "### Check: generated regression\n"
        "**Command run:** pytest test_timeout.py -q\n"
        "**Output observed:** 1 passed in 0.03s\n"
        "**Result: PASS**\n"
        "VERDICT: PASS"
    )
    provider = ScriptedProvider(
        [
            _tool_events(
                "Agent",
                {
                    "description": "verify timeout fix",
                    "prompt": agent_prompt,
                    "subagent_type": "verification",
                },
                "agent-call",
            ),
            _tool_events(
                "Bash",
                {"command": "pytest test_timeout.py -q"},
                "bash-call",
            ),
            _text_events(report),
            _text_events("Verifier passed; spot-check complete."),
        ]
    )
    tools = [
        _tool("Read"),
        _tool("Glob"),
        _tool("Grep"),
        _tool("Bash", BashInput, _bash),
        AGENT_TOOL,
    ]
    state = AgentState(
        cwd=str(tmp_path),
        messages=[UserMessage(content="fix the timeout")],
    )
    params = QueryParams(
        system="main system",
        model="test-model",
        max_tokens=256,
        provider=provider,
        abort_signal=asyncio.Event(),
        tools=tools,
        can_use_tool=default_can_use_tool,
        transcript_path=str(tmp_path / "main.jsonl"),
        verification_agent_max_turns=5,
    )

    _ = [item async for item in query_loop(state, params, NoopTracer())]

    assert len(provider.calls) == 4
    verifier_first = provider.calls[1]
    assert verifier_first["model"] == "test-model"
    assert {tool.name for tool in verifier_first["tools"]} == {
        "Read",
        "Glob",
        "Grep",
        "Bash",
    }
    assert len(verifier_first["messages"]) == 1
    assert verifier_first["messages"][0].content == agent_prompt
    assert "fix the timeout" not in str(verifier_first["messages"])

    verifier_second = provider.calls[2]
    verifier_tool_results = verifier_second["messages"][-1].content
    assert isinstance(verifier_tool_results[0], ToolResultBlock)
    assert "executed=pytest test_timeout.py -q" in verifier_tool_results[0].content

    parent_after_verifier = provider.calls[3]
    parent_tool_results = parent_after_verifier["messages"][-1].content
    assert isinstance(parent_tool_results[0], ToolResultBlock)
    assert "VERDICT: PASS" in parent_tool_results[0].content
    assert state.messages[-1].content[0].text == "Verifier passed; spot-check complete."
