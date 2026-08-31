from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import signal
import socket
import sys
from types import SimpleNamespace

import pytest

from core.agent_loop import AgentConfig, submit
from core.agents.verification_workflow import _bind_isolated_bash
from core.agents.workspace_guard import (
    build_workspace_guard,
    restricted_paths_for_workspace,
)
from core.builtin_tools import BASH_TOOL
from core.builtin_tools.bash import BashInput
from core.tools import CanUseDecision, ToolContext
from core.types import AgentState, ToolUseBlock, UserMessage
from core.verification.coordinator import (
    CoordinatorOutcome,
    CoordinatorRunRequest,
    CoordinatorStatus,
    VerificationCoordinator,
)
from core.verification.models import VerificationPolicy
from core.verification.workflow import (
    ArtifactReference,
    FailureSignature,
    IncidentBundle,
    SourceLocation,
)
from telemetry.events import TraceKind
from telemetry.tracer import NoopTracer


class _UnusedProvider:
    def __init__(self):
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
        raise AssertionError("ordinary main loop must not run in Coordinator mode")

    def count_tokens(self, messages):
        return 0


class _RecordingTracer:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def child(self, **ctx):
        return self


async def _allow_all(_tool_call):
    return CanUseDecision(allow=True)


def _isolated_bash(workspace: Path):
    return _bind_isolated_bash(
        [BASH_TOOL],
        workspace=workspace,
        workspace_ignore=(),
        run_id="isolated-agent-test",
        cycle=1,
        candidate_ref="candidate-test",
    )[0]


def _tool_context(workspace: Path) -> ToolContext:
    return ToolContext(
        tracer=NoopTracer(),
        abort_signal=asyncio.Event(),
        agent_state=AgentState(cwd=str(workspace)),
    )


@pytest.mark.asyncio
async def test_workspace_guard_rejects_file_and_bash_path_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    outside_root = tmp_path / "control"
    outside_root.mkdir()
    outside_file = outside_root / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    (workspace / "leak").symlink_to(outside_file)
    guard = build_workspace_guard(
        _allow_all,
        workspace=workspace,
        allowed_tool_names=frozenset({"Read", "Write", "Bash"}),
    )

    inside = await guard(
        ToolUseBlock(id="inside", name="Read", input={"file_path": "src/app.py"})
    )
    outside = await guard(
        ToolUseBlock(id="outside", name="Write", input={"file_path": "../control/x"})
    )
    bash_escape = await guard(
        ToolUseBlock(
            id="bash-outside",
            name="Bash", input={"command": f"pytest {outside_root}"},
        )
    )
    read_symlink_escape = await guard(
        ToolUseBlock(id="read-symlink", name="Read", input={"file_path": "leak"})
    )
    bash_symlink_escape = await guard(
        ToolUseBlock(id="bash-symlink", name="Bash", input={"command": "cat leak"})
    )

    assert inside.allow is True
    assert outside.allow is False
    assert bash_escape.allow is False
    assert read_symlink_escape.allow is False
    assert bash_symlink_escape.allow is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    (
        "pytest $HOME/outside/test_api.py",
        "pytest ${HOME}/outside/test_api.py",
        'pytest "$(printf /tmp)/test_api.py"',
        "pytest `printf /tmp`/test_api.py",
        "pytest ~/outside/test_api.py",
        "pytest {..,tests}/control/test_api.py",
        "pytest tests/*.py",
        "pytest <(printf test_api.py)",
    ),
)
async def test_workspace_guard_rejects_dynamic_bash_paths(
    tmp_path: Path, command: str
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    guard = build_workspace_guard(
        _allow_all,
        workspace=workspace,
        allowed_tool_names=frozenset({"Bash"}),
    )

    decision = await guard(
        ToolUseBlock(id="dynamic-path", name="Bash", input={"command": command})
    )

    assert decision.allow is False


@pytest.mark.asyncio
async def test_workspace_guard_allows_static_workspace_paths_and_env_values(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    (workspace / "tests").mkdir(parents=True)
    test_file = workspace / "tests" / "test_api.py"
    test_file.write_text("", encoding="utf-8")
    guard = build_workspace_guard(
        _allow_all,
        workspace=workspace,
        allowed_tool_names=frozenset({"Bash"}),
    )

    decision = await guard(
        ToolUseBlock(
            id="static-path",
            name="Bash",
            input={"command": "env TESTING=1 pytest tests/test_api.py"},
        )
    )

    assert decision.allow is True


@pytest.mark.asyncio
async def test_workspace_guard_blocks_restricted_subtree_for_read_and_search(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    source = workspace / "src"
    history = workspace / ".loop-engineer" / "learned-repair-skills"
    source.mkdir(parents=True)
    history.mkdir(parents=True)
    (source / "app.py").write_text("safe\n", encoding="utf-8")
    (history / "SKILL.md").write_text("historical fix\n", encoding="utf-8")
    guard = build_workspace_guard(
        _allow_all,
        workspace=workspace,
        allowed_tool_names=frozenset({"Read", "Glob", "Grep", "Write", "Bash"}),
        restricted_relative_paths=(
            ".loop-engineer/learned-repair-skills",
        ),
    )

    forbidden_calls = (
        ToolUseBlock(
            id="read-history",
            name="Read",
            input={"file_path": ".loop-engineer/learned-repair-skills/SKILL.md"},
        ),
        ToolUseBlock(
            id="glob-history",
            name="Glob",
            input={
                "pattern": "**/*",
                "path": ".loop-engineer/learned-repair-skills",
            },
        ),
        ToolUseBlock(
            id="grep-history",
            name="Grep",
            input={
                "pattern": "fix",
                "path": ".loop-engineer/learned-repair-skills",
            },
        ),
        # A recursive root search would otherwise traverse the restricted child.
        ToolUseBlock(
            id="glob-root",
            name="Glob",
            input={"pattern": "**/*"},
        ),
        ToolUseBlock(
            id="grep-root",
            name="Grep",
            input={"pattern": "fix", "path": "."},
        ),
        ToolUseBlock(
            id="bash-history",
            name="Bash",
            input={"command": "pytest .loop-engineer/learned-repair-skills"},
        ),
        ToolUseBlock(
            id="write-history",
            name="Write",
            input={
                "file_path": ".loop-engineer/learned-repair-skills/SKILL.md",
                "content": "poisoned",
            },
        ),
        ToolUseBlock(
            id="implicit-rg-history",
            name="Bash",
            input={"command": "rg --hidden historical"},
        ),
        ToolUseBlock(
            id="implicit-rg-all-history",
            name="Bash",
            input={"command": "rg -uu historical"},
        ),
        ToolUseBlock(
            id="implicit-git-grep-history",
            name="Bash",
            input={"command": "git grep --no-index historical"},
        ),
        ToolUseBlock(
            id="git-revision-history",
            name="Bash",
            input={
                "command": (
                    "git show HEAD:.loop-engineer/learned-repair-skills/SKILL.md"
                )
            },
        ),
    )

    forbidden_decisions = [await guard(call) for call in forbidden_calls]
    assert all(not decision.allow for decision in forbidden_decisions)
    assert (
        await guard(
            ToolUseBlock(
                id="read-source",
                name="Read",
                input={"file_path": "src/app.py"},
            )
        )
    ).allow
    assert (
        await guard(
            ToolUseBlock(
                id="grep-source",
                name="Grep",
                input={"pattern": "safe", "path": "src"},
            )
        )
    ).allow


def test_workspace_guard_rejects_invalid_restricted_relative_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()

    for invalid in ("", ".", "../history", str(tmp_path / "history")):
        with pytest.raises(ValueError, match="restricted workspace path"):
            build_workspace_guard(
                _allow_all,
                workspace=workspace,
                allowed_tool_names=frozenset({"Read"}),
                restricted_relative_paths=(invalid,),
            )


def test_control_plane_roots_are_mapped_or_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()

    assert restricted_paths_for_workspace(workspace, (tmp_path / "outside",)) == ()

    for protected in (workspace / "operator" / "skills", tmp_path):
        with pytest.raises(ValueError, match="protected control-plane root"):
            restricted_paths_for_workspace(workspace, (protected,))


@pytest.mark.asyncio
async def test_workflow_bash_uses_snapshot_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    original = workspace / "locked.txt"
    original.write_text("original", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("LOOP_ENGINEER_SIGNING_KEY", "signing-secret")
    script = """
import os
from pathlib import Path
print(os.getenv("GITHUB_TOKEN"))
print(os.getenv("LOOP_ENGINEER_SIGNING_KEY"))
try:
    Path("locked.txt").write_text("changed")
except OSError:
    print("LOCKED")
Path("generated.txt").write_text("temporary")
print(Path("generated.txt").read_text())
"""
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    output = await _isolated_bash(workspace).func(
        BashInput(command=command), _tool_context(workspace)
    )

    assert output.splitlines()[:2] == ["None", "None"]
    assert "LOCKED" in output
    assert "temporary" in output
    assert "github-secret" not in output
    assert "signing-secret" not in output
    assert original.read_text(encoding="utf-8") == "original"
    assert not (workspace / "generated.txt").exists()


@pytest.mark.asyncio
async def test_workflow_bash_does_not_interpret_shell_control(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    escaped = tmp_path / "shell-injection.txt"
    command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"safe\")' ; "
        f"/usr/bin/touch {shlex.quote(str(escaped))}"
    )

    output = await _isolated_bash(workspace).func(
        BashInput(command=command), _tool_context(workspace)
    )

    assert "safe" in output
    assert not escaped.exists()


@pytest.mark.asyncio
async def test_workflow_bash_sandbox_blocks_external_files_and_network(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("host-secret", encoding="utf-8")
    escaped = tmp_path / "escaped.txt"
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    script = """
import socket
import sys
from pathlib import Path
try:
    print("READ=" + Path(sys.argv[1]).read_text())
except OSError:
    print("READ=BLOCKED")
try:
    Path(sys.argv[2]).write_text("escaped")
    print("WRITE=ALLOWED")
except OSError:
    print("WRITE=BLOCKED")
try:
    socket.create_connection(("127.0.0.1", int(sys.argv[3])), timeout=1)
    print("NETWORK=ALLOWED")
except OSError:
    print("NETWORK=BLOCKED")
"""
    command = " ".join(
        (
            shlex.quote(sys.executable),
            "-c",
            shlex.quote(script),
            shlex.quote(str(outside)),
            shlex.quote(str(escaped)),
            str(port),
        )
    )
    try:
        output = await _isolated_bash(workspace).func(
            BashInput(command=command), _tool_context(workspace)
        )
    finally:
        server.close()

    assert "READ=BLOCKED" in output
    assert "WRITE=BLOCKED" in output
    assert "NETWORK=BLOCKED" in output
    assert "host-secret" not in output
    assert not escaped.exists()


@pytest.mark.asyncio
async def test_workflow_bash_timeout_uses_process_group_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("process-group cleanup is POSIX-specific")
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    from core.verification import runner as runner_module

    cleanup_calls: list[tuple[int, int]] = []
    original_killpg = runner_module.os.killpg

    def recording_killpg(pid: int, sig: int) -> None:
        cleanup_calls.append((pid, sig))
        original_killpg(pid, sig)

    monkeypatch.setattr(runner_module.os, "killpg", recording_killpg)
    script = """
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(30)
"""
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with pytest.raises(ValueError, match="process group terminated"):
        await _isolated_bash(workspace).func(
            BashInput(command=command, timeout=100), _tool_context(workspace)
        )

    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][1] == signal.SIGKILL


def _run_request(tmp_path: Path) -> CoordinatorRunRequest:
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    control.mkdir()
    candidate.mkdir()
    artifact = ArtifactReference(uri="file:///evidence/a", sha256="a" * 64)
    return CoordinatorRunRequest(
        run_id="public-entry",
        incident=IncidentBundle(
            incident_id="incident-1",
            requirement="repair timeout",
            matched_rule="timeout",
            error_logs=(artifact,),
            original_trace=artifact,
            source_locations=(
                SourceLocation(path="service.py", start_line=1, revision="control"),
            ),
            root_cause="deadline was reused",
            control_ref="control",
            original_input={"prompt": "checkout"},
            failure_signature=FailureSignature(
                code="checkout.timeout", error_type="TimeoutError"
            ),
        ),
        control_workspace=str(control),
        candidate_workspace=str(candidate),
    )


def test_agent_config_rejects_incident_workflow_fields(tmp_path: Path) -> None:
    """The incident workflow no longer rides on a normal agent session.

    submit()/AgentConfig used to accept a coordinator + run request and branch into
    the whole diagnose->repair->verify->release loop. That path is removed; the loop
    is driven by the LoopEngineer orchestrator / VerificationCoordinator directly.
    """

    for field in (
        "verification_coordinator",
        "verification_run_request",
        "verification_release_action",
        "verification_escalation_handler",
    ):
        with pytest.raises(TypeError):
            AgentConfig(
                provider=_UnusedProvider(),
                system="main system",
                model="test-model",
                max_tokens=1024,
                cwd=str(tmp_path),
                **{field: object()},
            )

    import dataclasses

    names = {f.name for f in dataclasses.fields(AgentConfig)}
    assert not (
        names
        & {
            "verification_coordinator",
            "verification_run_request",
            "verification_release_action",
            "verification_escalation_handler",
        }
    )


def test_submit_has_no_coordinator_entrypoint() -> None:
    import core.agent_loop as agent_loop

    assert not hasattr(agent_loop, "_submit_with_verification_coordinator")
