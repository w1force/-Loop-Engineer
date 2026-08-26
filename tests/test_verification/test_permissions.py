"""Verification 工具白名单、动态命令和注册开关。"""

import asyncio

import pytest
from pydantic import ValidationError

from core.agent_loop import AgentConfig, build_system_prompt
from core.agents.verification import build_verification_can_use_tool
from core.builtin_tools import BASH_TOOL
from core.builtin_tools.agent import AgentInput
from core.tool_executor import make_executor
from core.tools import CanUseDecision, ToolContext, default_can_use_tool
from core.types import AgentState, ToolUseBlock
from telemetry.tracer import NoopTracer


class NoopProvider:
    def stream(self, **kwargs):
        raise NotImplementedError

    def count_tokens(self, messages):
        return 0


async def test_agent_tool_registration_is_config_gated():
    disabled = AgentConfig(
        provider=NoopProvider(), system="base", model="m", max_tokens=32
    )
    enabled = AgentConfig(
        provider=NoopProvider(),
        system="base",
        model="m",
        max_tokens=32,
        verification_agent_enabled=True,
    )

    assert "Agent" not in {tool.name for tool in await disabled.resolve_tools()}
    assert "Agent" in {tool.name for tool in await enabled.resolve_tools()}


def test_main_prompt_contract_is_config_gated():
    disabled = AgentConfig(
        provider=NoopProvider(), system="base", model="m", max_tokens=32
    )
    enabled = AgentConfig(
        provider=NoopProvider(),
        system="base",
        model="m",
        max_tokens=32,
        verification_agent_enabled=True,
    )

    assert "Independent verification contract" not in build_system_prompt(
        AgentState(), disabled
    )
    prompt = build_system_prompt(AgentState(), enabled)
    assert "Independent verification contract" in prompt
    assert 'subagent_type="verification"' in prompt
    assert "machine-enforced completion gate" in prompt


def test_agent_input_schema_restricts_subagent_type_and_extra_fields():
    valid = AgentInput(
        description="verify fix",
        prompt="original task and changed files",
        subagent_type="verification",
    )
    assert valid.subagent_type == "verification"

    with pytest.raises(ValidationError):
        AgentInput.model_validate(
            {
                "description": "implement",
                "prompt": "edit files",
                "subagent_type": "general-purpose",
            }
        )
    with pytest.raises(ValidationError):
        AgentInput.model_validate(
            {
                "description": "verify",
                "prompt": "check",
                "subagent_type": "verification",
                "run_in_background": True,
            }
        )


async def test_verifier_allows_dynamic_local_validation_commands():
    policy = build_verification_can_use_tool(default_can_use_tool)
    commands = [
        "uv run pytest tests/test_api.py -q",
        "npm run typecheck",
        "bun test tests/hash.test.ts",
        "cargo clippy",
        "python -c 'print(0)'",
        "curl -s http://127.0.0.1:8080/health",
    ]

    for index, command in enumerate(commands):
        decision = await policy(
            ToolUseBlock(
                id=f"bash-{index}",
                name="Bash",
                input={"command": command},
            )
        )
        assert decision.allow, (command, decision.reason)


async def test_verifier_executes_model_generated_bash_command(tmp_path):
    policy = build_verification_can_use_tool(default_can_use_tool)
    ctx = ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(cwd=str(tmp_path)),
    )
    executor = make_executor(
        "batch", [BASH_TOOL], policy, NoopTracer(), ctx
    )
    executor.add_tool(
        ToolUseBlock(
            id="dynamic-command",
            name="Bash",
            input={"command": "python3 -c 'print(12345)'"},
        )
    )

    result = (await executor.get_results())[0]
    assert result.is_error is False
    assert result.content == "12345"


async def test_verifier_denies_mutation_tools_and_unsafe_bash():
    policy = build_verification_can_use_tool(default_can_use_tool)
    for name in ["Edit", "Write", "Agent", "Load_Skill", "LSP"]:
        decision = await policy(
            ToolUseBlock(id=name, name=name, input={})
        )
        assert decision.allow is False

    commands = [
        "rm -rf .",
        "npm install left-pad",
        "echo changed > service.py",
        "curl -s https://example.com/api",
        "pytest -q && git status",
    ]
    for index, command in enumerate(commands):
        decision = await policy(
            ToolUseBlock(
                id=f"unsafe-{index}",
                name="Bash",
                input={"command": command},
            )
        )
        assert decision.allow is False, command


async def test_verifier_does_not_override_custom_parent_denial():
    async def deny_all(_tool_call):
        return CanUseDecision(allow=False, reason="session policy denied")

    policy = build_verification_can_use_tool(deny_all)
    decision = await policy(
        ToolUseBlock(
            id="custom-deny",
            name="Bash",
            input={"command": "python3 -c 'print(1)'"},
        )
    )

    assert decision.allow is False
    assert decision.reason == "session policy denied"
