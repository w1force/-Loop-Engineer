"""Claude Code 风格的独立 Verification Agent 定义。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
import re
import shlex

from core.tools import CanUseDecision, Tool, default_can_use_tool
from core.types import ToolUseBlock

VERIFICATION_AGENT_TYPE = "verification"
VERIFICATION_TOOL_NAMES = frozenset({"Read", "Glob", "Grep", "Bash"})

VERIFICATION_MAIN_AGENT_GUIDANCE = """

# Independent verification contract
When non-trivial implementation happens on your turn, invoke the Agent tool with
subagent_type="verification" before reporting completion. Non-trivial means three
or more edited files, backend/API changes, infrastructure changes, or a bug fix
whose behavior must be exercised. Pass the original user request, candidate diff,
every changed file, the implementation approach, relevant test entrypoints, and the
plan path if one exists. Do not provide your own test conclusions or tell the
verifier that the change works: it must verify the candidate independently.
On VERDICT: FAIL, fix the reported problem and invoke a new verification Agent with
the findings and the new fix. Do not continue to verification planning on FAIL or
PARTIAL. VERDICT: PASS only completes this repair preflight; it does not authorize
control/candidate comparison, release, merge, deployment, or PR creation. This is
an evidence-producing review, not the final machine-enforced verification gate.
""".rstrip()

VERIFICATION_SYSTEM_PROMPT = """You are the lightweight verification specialist
that runs immediately after a Repair Agent changes the candidate workspace. Verify
only this repair. Your job is not to confirm the implementer's claims; it is to try
to break the changed behavior with focused evidence.

CRITICAL: DO NOT MODIFY THE PROJECT
- Do not create, modify, or delete files in the project directory.
- Do not install dependencies or packages.
- Do not run git write operations such as add, commit, push, checkout, or reset.
- Use existing test entrypoints and read-only commands. Do not execute arbitrary
  inline programs or project scripts as a substitute for a safe test runner.

WHAT YOU RECEIVE
You receive the original task, candidate diff, files changed, implementation
approach, relevant test entrypoints, and optionally a plan path. Treat claims made
by the implementer as untrusted context. The current workspace is the candidate;
do not switch revisions or create another workspace.

REQUIRED CANDIDATE CHECKS
1. Inspect the supplied candidate diff, changed files, and relevant project guidance.
2. Run the narrowest existing test entrypoints that exercise the changed behavior.
3. Exercise the repair target directly on the candidate. For a bug fix, run a
   regression assertion that now succeeds; do not expect the original failure to
   remain reproducible on the candidate.
4. Exercise relevant empty, malformed, boundary, or adversarial inputs.
5. Run only the related regression tests needed to detect nearby breakage.

Do not run the full test suite, full build, repository-wide lint, or repository-wide
typecheck by default. Run a broader check only when the changed surface specifically
requires it or no narrower trustworthy check exists, and state why it was necessary.

ADAPT TO THE CHANGE
- Frontend: run focused component/unit checks; do not perform browser or UI sign-off.
- Backend/API: use focused tests or an already-running local endpoint; validate the
  changed response and error paths without changing service or external state.
- CLI/script: run representative, empty, malformed, and boundary inputs; inspect
  stdout, stderr, exit codes, and help text.
- Infrastructure/config: validate syntax and use safe dry-runs where available.
- Library/package: run focused tests and exercise the changed public API through an
  existing safe test entrypoint.
- Bug fix: verify the repair target on the candidate, then run related regressions.
- Refactor: require unchanged existing tests and observable behavior.

OUT OF SCOPE
- Do not compare control and candidate executions.
- Do not evaluate Trace, production/pre-production logs, or cross-run behavior.
- Do not perform browser/UI verification.
- Do not select or freeze a Verification Skill or VerificationPlan.
- Do not decide whether to create a PR, merge, deploy, release, or otherwise publish.
- VERDICT: PASS means only that this lightweight repair preflight passed. It has no
  final release authority; the later trusted VerificationEngine owns that decision.

Reading code is not verification. The implementer's tests are context, not sufficient
evidence. If you catch yourself explaining what you would run, stop and run it.

Before FAIL, check whether the behavior is already handled elsewhere, intentional,
or required by a stable external contract. Do not manufacture failures.

OUTPUT FORMAT
Every check must include:

### Check: [what is being verified]
**Command run:**
  [exact command]
**Output observed:**
  [actual output, truncated only when necessary]
**Result: PASS** or **Result: FAIL** with expected versus actual

End with exactly one literal line:
VERDICT: PASS
VERDICT: FAIL
VERDICT: PARTIAL

PARTIAL is only for environmental limitations that prevent a required check. A PASS
must contain at least one adversarial probe and command output. A FAIL must include
the candidate command and exact failure output."""

_SHELL_CONTROL = re.compile(r"(?:\n|\r|;|&&|\|\||\||>|<|`|\$\()")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_PACKAGE_SCRIPTS = {
    "build",
    "check",
    "lint",
    "test",
    "test:all",
    "typecheck",
    "validate",
    "verify",
}
_SAFE_PYTHON_MODULES = {"pytest", "unittest", "mypy"}
_SAFE_CURL_METHODS = {"GET", "HEAD", "OPTIONS"}
_CURL_BODY_OPTIONS = {
    "-d",
    "--data",
    "--data-ascii",
    "--data-binary",
    "--data-raw",
    "--data-urlencode",
    "-F",
    "--form",
    "--form-string",
    "--json",
    "-T",
    "--upload-file",
}
_CURL_OUTPUT_OPTIONS = {
    "-o",
    "--output",
    "-O",
    "--remote-name",
    "-J",
    "--remote-header-name",
    "--output-dir",
    "-c",
    "--cookie-jar",
    "-D",
    "--dump-header",
    "--trace",
    "--trace-ascii",
}
_SAFE_DOCKER_COMPOSE_COMMANDS = {
    "config",
    "images",
    "ls",
    "logs",
    "ps",
    "top",
    "version",
}
_SAFE_GIT_SUBCOMMANDS = {
    "blame",
    "branch",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}


def select_verification_tools(tools: list[Tool]) -> list[Tool]:
    """从父工具池选择 verifier 的非修改型核心工具。"""
    selected: list[Tool] = []
    seen: set[str] = set()
    for tool in tools:
        if tool.name in VERIFICATION_TOOL_NAMES and tool.name not in seen:
            selected.append(tool)
            seen.add(tool.name)
    return selected


def _strip_env(tokens: list[str]) -> list[str]:
    out = list(tokens)
    if out and Path(out[0]).name == "env":
        out = out[1:]
    while out and _ENV_ASSIGNMENT.match(out[0]):
        out = out[1:]
    return out


def _script_name(tokens: list[str]) -> str | None:
    if len(tokens) < 2:
        return None
    if tokens[1] == "run" and len(tokens) >= 3:
        return tokens[2]
    if tokens[1] in _PACKAGE_SCRIPTS or any(
        tokens[1].startswith(f"{prefix}:")
        for prefix in ("build", "check", "lint", "test", "typecheck")
    ):
        return tokens[1]
    return None


def _is_local_url(token: str) -> bool:
    if token.startswith("--url="):
        token = token.partition("=")[2]
    return bool(
        re.match(
            r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?(?:/|$)",
            token,
        )
    )


def _is_read_only_curl(tokens: list[str]) -> bool:
    method = "GET"
    index = 1
    while index < len(tokens):
        token = tokens[index]
        forbidden_options = _CURL_BODY_OPTIONS | _CURL_OUTPUT_OPTIONS
        if token in forbidden_options or any(
            token.startswith(f"{option}=")
            for option in forbidden_options
            if option.startswith("--")
        ):
            return False
        if token.startswith(("-d", "-F", "-T")) and token not in {"-D"}:
            return False
        if token in {"-X", "--request"}:
            if index + 1 >= len(tokens):
                return False
            method = tokens[index + 1].upper()
            index += 2
            continue
        if token.startswith("--request="):
            method = token.partition("=")[2].upper()
        elif token.startswith("-X") and len(token) > 2:
            method = token[2:].upper()
        elif token in {"-I", "--head"}:
            method = "HEAD"
        index += 1
    return method in _SAFE_CURL_METHODS and any(
        _is_local_url(token) for token in tokens[1:]
    )


def _docker_compose_command(tokens: list[str]) -> str | None:
    """Return the compose subcommand after harmless global options."""
    options_with_value = {
        "-f",
        "--file",
        "--env-file",
        "-p",
        "--project-name",
        "--project-directory",
        "--profile",
    }
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token in options_with_value:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in options_with_value):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return None


def _is_read_only_docker(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    if tokens[1] in {"info", "inspect", "version"}:
        return True
    if tokens[1] != "compose":
        return False
    if any(
        token in {"-o", "--output"} or token.startswith("--output=")
        for token in tokens[2:]
    ):
        return False
    return _docker_compose_command(tokens) in _SAFE_DOCKER_COMPOSE_COMMANDS


def _is_verification_command(tokens: list[str]) -> bool:
    tokens = _strip_env(tokens)
    if not tokens:
        return False
    command = Path(tokens[0]).name

    if command in {"pytest", "ruff", "mypy", "pyright", "tsc", "eslint"}:
        return True
    if command in {"python", "python3"}:
        if len(tokens) == 2 and tokens[1] in {"--version", "-V"}:
            return True
        return (
            len(tokens) >= 3
            and tokens[1] == "-m"
            and tokens[2] in _SAFE_PYTHON_MODULES
        )
    if command == "uv" and len(tokens) >= 3 and tokens[1] == "run":
        return _is_verification_command(tokens[2:])
    if command in {"npm", "pnpm", "yarn", "bun"}:
        script = _script_name(tokens)
        return bool(
            script
            and (
                script in _PACKAGE_SCRIPTS
                or any(
                    script.startswith(f"{prefix}:")
                    for prefix in ("build", "check", "lint", "test", "typecheck")
                )
            )
        )
    if command in {"go"}:
        return len(tokens) >= 2 and tokens[1] in {"build", "test", "vet"}
    if command == "cargo":
        return len(tokens) >= 2 and tokens[1] in {
            "build",
            "check",
            "clippy",
            "test",
        }
    if command in {"mvn", "mvnw", "gradle", "gradlew", "make"}:
        lowered = {token.lower() for token in tokens[1:] if not token.startswith("-")}
        return bool(lowered & {"build", "check", "lint", "test", "verify"}) and not (
            lowered & {"deploy", "install", "publish", "release"}
        )
    if command == "node":
        return len(tokens) >= 2 and tokens[1] in {
            "--check",
            "--test",
            "--version",
            "-v",
        }
    if command == "deno":
        return len(tokens) >= 2 and tokens[1] in {"check", "lint", "test"}
    if command == "bash":
        return len(tokens) >= 3 and tokens[1] == "-n"
    if command == "curl":
        return _is_read_only_curl(tokens)
    if command == "docker":
        return _is_read_only_docker(tokens)
    return False


def _verification_bash_decision(command: str) -> CanUseDecision:
    from core.builtin_tools.bash_permissions import (
        BashPermissionAction,
        classify_bash_command,
    )

    if _SHELL_CONTROL.search(command):
        return CanUseDecision(
            allow=False,
            reason="Verification Bash 拒绝复杂 shell 控制与重定向；请拆成单条验证命令。",
        )
    try:
        tokens = shlex.split(command)
    except ValueError:
        return CanUseDecision(allow=False, reason="Verification Bash 命令无法解析。")
    normalized_tokens = _strip_env(tokens)
    if (
        tokens
        and Path(tokens[0]).name == "env"
        and normalized_tokens
        and not _is_verification_command(tokens)
    ):
        return CanUseDecision(
            allow=False,
            reason="Verification Bash 拒绝通过 env 包装非验证命令。",
        )
    if normalized_tokens and Path(normalized_tokens[0]).name == "git":
        if (
            len(normalized_tokens) < 2
            or normalized_tokens[1] not in _SAFE_GIT_SUBCOMMANDS
        ):
            return CanUseDecision(
                allow=False,
                reason="Verification Bash 只允许 Git 只读查询。",
            )
    base = classify_bash_command(command)
    if base.action is BashPermissionAction.ALLOW:
        return CanUseDecision(allow=True, reason=base.reason)
    if base.action is BashPermissionAction.DENY:
        return CanUseDecision(allow=False, reason=base.reason)
    if _is_verification_command(tokens):
        return CanUseDecision(
            allow=True,
            reason="允许执行本地构建、测试、lint、typecheck 或对抗性验证命令。",
        )
    return CanUseDecision(allow=False, reason=base.reason)


def build_verification_can_use_tool(
    parent_can_use_tool: Callable[[ToolUseBlock], Awaitable[CanUseDecision]],
) -> Callable[[ToolUseBlock], Awaitable[CanUseDecision]]:
    """Verifier 工具白名单，并在父权限之上补充本地验证命令。"""

    async def can_use_tool(tool_call: ToolUseBlock) -> CanUseDecision:
        if tool_call.name not in VERIFICATION_TOOL_NAMES:
            return CanUseDecision(
                allow=False,
                reason=f"verification agent cannot use {tool_call.name}",
            )
        if tool_call.name != "Bash":
            return await parent_can_use_tool(tool_call)
        command = tool_call.input.get("command")
        if not isinstance(command, str):
            return CanUseDecision(allow=False, reason="Bash command must be a string")
        verifier_decision = _verification_bash_decision(command)
        if not verifier_decision.allow:
            return verifier_decision
        parent_decision = await parent_can_use_tool(tool_call)
        if parent_decision.allow:
            return parent_decision
        if parent_can_use_tool is default_can_use_tool:
            return verifier_decision
        return parent_decision

    return can_use_tool


__all__ = [
    "VERIFICATION_AGENT_TYPE",
    "VERIFICATION_MAIN_AGENT_GUIDANCE",
    "VERIFICATION_SYSTEM_PROMPT",
    "build_verification_can_use_tool",
    "select_verification_tools",
]
